"""Book-scale director pass, and the data the direction review screen shows.

The gateway director reads *one* passage and configures every engine at once.
This module is the same idea at the scale of a book, with three changes that are
the whole design:

**Scene-level, never per line.** A thousand-utterance book is a thousand LLM
calls per line — hours of GPU, and the notes drift: line 4 gets "urgent, clipped"
and line 5 gets "warm and slow" because the model never saw them together. One
call per *scene* is ~40 calls for a novel chapter, and the note is coherent
because the model read the whole exchange. The scene note is then stamped onto
every line in the scene that is routed to an instruction-capable engine.

**Nothing here validates the model's words.** The anti-paraphrase rule — strip
the tags and what remains must be the book, character for character — lives in
``gateway/app/services/director.py`` and runs server-side, once, for every
caller. This module consumes the verdict (``text`` / ``text_status`` /
``rejected_text``) and never second-guesses it. What it *does* add is
**distribution**: the gateway validates a whole scene passage, and a renderer
needs per-utterance text, so the annotated scene is walked back onto the
individual lines and each line is re-checked (stripping its tags must reproduce
that line). A scene that will not line up is rejected whole — a tag placed on
the wrong line is a wrong take, and silently so.

**A pass is a document, not an action.** ``run_pass`` produces a ``Pass`` full of
proposals with ``decision: "pending"``; ``apply_pass`` writes the accepted ones
into the script. Nothing reaches the book until someone (or ``--apply``) decides.
Checkpointing after every scene and a content-addressed cache make a 400-scene
run resumable and a re-run nearly free.

Types match ``docs/STUDIO-API.md`` (``Pass`` / ``PassScene`` / ``PassCounts``) —
the studio service persists these verbatim as ``passes/<pass_id>.json``.

Dependency note: this file is stdlib + pydantic only, and imports nothing from
the repo root. The studio image is built from ``./pipeline`` alone (see
``pipeline/Dockerfile``: "self-contained; imports nothing from the repo root"),
so an ``import gateway...`` or ``import httpx`` here would build fine on a dev box
and ``ModuleNotFoundError`` inside the container. The gateway is reached over
HTTP with ``urllib``, exactly like ``render.http_synth``.

CLI
---
    python -m pipeline.direct pipeline/samples/dialogue.txt --out pass.json --fake
    python -m pipeline.direct script.json --out pass.json --gateway http://localhost:8080 \
        --model gemma3:12b --target-engine breeze
    python -m pipeline.direct book.txt --out pass.json --fake --apply --script-out script.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .render import DEFAULT_GATEWAY
from .schema import WIRE_SCHEMA, BookScript

# An engine spec as ``gateway.app.models.DirectorEngineSpec`` serializes it.
# A plain dict, not the pydantic model, because the model lives in the gateway
# package and this module cannot import it (see the note above).
EngineSpecDict = dict[str, Any]

# The gateway's ``POST /api/director/annotate`` in one callable: passage text +
# engine specs -> the parsed JSON response. Everything that talks to a model goes
# through this seam, which is why the whole module tests offline.
AnnotateFn = Callable[[str, list[EngineSpecDict]], dict]

FAKE_MODEL = "fake-director"
FAKE_PROMPT_VERSION = "fake"

_MAX_INSTRUCTION_CHARS = 400  # mirrors the gateway's budget for a note

# Offline stand-ins for the specs the gateway serves at /api/engines. Used by
# --fake and as a fallback when no gateway is reachable; the real path fetches
# the live ones (``engine_spec_from_gateway``) so a tag list edited in the engine
# does not have to be edited here too.
DEFAULT_SPECS: dict[str, EngineSpecDict] = {
    "breeze": {
        "id": "breeze",
        "name": "Breeze TTS 2",
        "emotion_labels": [],
        "voices": [],
        # The four events the open weights actually attest. `(gasp)`, `(whisper)`
        # and `(pause)` used to sit here on the strength of BreezeBlue's *hosted*
        # docs, which describe a different model — see
        # docs/BREEZE-AUDIT-2026-09-05.md. A fallback that advertises tags the
        # checkpoint never saw teaches the offline fake director to expect them.
        "audio_tags": ["(laugh)", "(cough)", "(clears throat)", "(sigh)"],
        "emotion_scalar": True,
        "emotion_tags": True,
        "instruction": True,
    },
    "qwen3tts": {
        "id": "qwen3tts",
        "name": "Qwen3-TTS",
        "emotion_labels": [],
        # engines/qwen3tts/tts.py: the CustomVoice speakers, default first.
        "voices": ["aiden", "ryan", "vivian", "serena", "uncle_fu", "dylan", "eric", "ono_anna", "sohee"],
        # Nothing in the open weights attests an inline event vocabulary, so
        # the director must never insert one here.
        "audio_tags": [],
        "emotion_scalar": False,
        "emotion_tags": False,
        "instruction": True,  # CustomVoice 1.7B `instruct`
    },
    "indextts2": {
        "id": "indextts2",
        "name": "IndexTTS 2",
        "emotion_labels": ["neutral", "happy", "sad", "angry", "afraid", "surprised", "calm"],
        "voices": [],
        "audio_tags": [],  # IndexTTS2 has no inline tag vocabulary
        "emotion_scalar": True,
        "emotion_tags": True,
        "instruction": True,
    },
}


class TagDistributionError(ValueError):
    """The annotated scene could not be walked back onto its individual lines."""


# ---- text normalization -----------------------------------------------------

def _squash(text: str) -> str:
    """Normalized form for comparing two versions of a passage.

    A deliberate copy of ``gateway.app.services.director._squash`` — the gateway
    is the source of truth, but this module cannot import it (container
    boundary, see the module docstring). ``pipeline/tests/test_direct.py`` pins
    the two implementations against each other whenever the gateway package is
    importable, so a drift fails a test rather than silently rejecting good
    annotations.

    Collapses whitespace runs, then closes up space in front of punctuation:
    removing a tag from ``again (pause). Outside`` leaves ``again . Outside``,
    which is an artifact of *our* stripping, not a change to the author's words.
    """
    collapsed = " ".join(text.split())
    return re.sub(r"\s+([,.;:!?…)\]}»”’\"'])", r"\1", collapsed)


def _tag_pattern(tags: Sequence[str]) -> re.Pattern | None:
    """Longest-first alternation, so ``(clears throat)`` wins over a prefix."""
    ordered = sorted({t for t in tags if t}, key=len, reverse=True)
    if not ordered:
        return None
    return re.compile("|".join(re.escape(t) for t in ordered))


def _clean_instruction(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text or text.lower() in ("null", "none", "n/a"):
        return None
    return text[:_MAX_INSTRUCTION_CHARS]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# The director's pace lives in a narrow band on purpose: it multiplies the
# profile's own base speed, and past ±20% a "reading" becomes a different book.
MIN_PACE, MAX_PACE = 0.8, 1.2
# What the *renderer* will accept once base × pace is worked out.
MIN_SPEED, MAX_SPEED = 0.7, 1.3


def clamp_pace(value: Any) -> float | None:
    """Coerce a director's pace into 0.8-1.2, or ``None`` if it isn't a number.

    Mirrors ``gateway.app.services.director._clean_pace`` (the gateway clamps its
    own replies; this module cannot import it — container boundary, see the
    module docstring) so a fake director, an older gateway, or a hand-edited pass
    all land in the same band.
    """
    if value is None:
        return None
    try:
        pace = float(value)
    except (TypeError, ValueError):
        return None
    if pace != pace or pace in (float("inf"), float("-inf")):
        return None
    return round(max(MIN_PACE, min(MAX_PACE, pace)), 3)


def instruction_is_inert(engine_id: str, instruction_engines: Sequence[str] | set[str]) -> bool:
    """Would a written note on a line routed to ``engine_id`` do nothing?

    True when the engine does not declare ``instruction`` — Kokoro, today, which
    is every line of the only licence-clean book this repo can publish. The note
    is then never stamped (``apply_pass`` skips it), but a *person* can still
    type one into the Inspector, and the studio should say so: the line goes
    ``stale`` and costs a synth call that produces byte-identical audio, because
    ``render.py`` puts the instruction in the cache key unconditionally.

    This is the warning seam; the cache key itself is
    deliberately left alone, since narrowing it per engine would silently change
    the identity of every already-rendered directed line.
    """
    return engine_id not in set(instruction_engines)


# ---- scene passages ---------------------------------------------------------

class PassageLine(BaseModel):
    """One utterance, reduced to what the director pass needs."""

    id: str
    text: str
    speaker: str = "Narrator"
    kind: str = "narration"
    paragraph: str = ""
    """The ``p`` component of the utterance id (``c0_p3_s1`` -> ``"3"``). Lines
    sharing it were one paragraph in the source and are joined with a space."""


class ScenePassage(BaseModel):
    """A scene as the director sees it: one block of prose, plus the line ids in
    order so an annotated version can be mapped back onto utterances."""

    scene_id: str
    chapter_index: int
    scene_index: int
    chapter_title: str = ""
    text: str
    lines: list[PassageLine] = Field(default_factory=list)

    @property
    def line_ids(self) -> list[str]:
        """**Every** line in the scene, directed or not.

        The studio's scene rail is built from this (``store.Derived.chapters``)
        and the contract's invariant is that the scenes of a chapter cover its
        lines exactly once — so a line that is not directed still belongs to its
        scene. Use ``directed_lines`` for "what the director sees".
        """
        return [line.id for line in self.lines]

    @property
    def directed_lines(self) -> list[PassageLine]:
        """The lines that make up ``text`` — everything but ``SKIPPED_KINDS``.

        This is the list ``distribute_tags`` walks against the annotated passage
        and the list ``apply_pass`` stamps, so all three agree on which lines the
        director was actually shown.
        """
        return [line for line in self.lines if line.kind not in SKIPPED_KINDS]

    @property
    def directed_line_ids(self) -> list[str]:
        return [line.id for line in self.directed_lines]


SKIPPED_KINDS = frozenset({"heading"})
"""Utterance kinds a director pass never sees and never stamps.

A ``heading`` is the chapter announcement, not prose: ``build.py`` synthesizes it
from ``Chapter.title`` and the narrator reads it in the narrator's voice with a
long pause after it. Leaving it in the scene passage did two wrong things at
once. The model was handed ``"Chapter One: The Ledger\\n\\nIt was 1943, and…"`` and
asked to direct it as narration — a tag may legitimately be placed on it, and
``distribute_tags`` would then attach that tag to the announcement. And the
scene's ``pace`` landed on it: in a trace recorded against a stub gateway the title
"Chapter One: The Ledger" was synthesized at speed 0.92 because a tense exchange
*later in the same scene* asked for 0.92. The title of a chapter is not
performed by the scene it introduces.
"""

_PARAGRAPH_RE = re.compile(r"^c\d+_p(\d+)")


def _paragraph_of(utterance_id: str) -> str:
    """Paragraph key from an utterance id built by ``pipeline.build``.

    Ids that don't match the ``c{ch}_p{para}_s{sent}`` shape get ``""``, which
    groups them with their neighbours rather than exploding a scene into
    one-line paragraphs.
    """
    match = _PARAGRAPH_RE.match(utterance_id or "")
    return match.group(1) if match else ""


def scene_passages(script: BookScript) -> list[ScenePassage]:
    """Group a script's utterances into one prose passage per scene.

    Grouping key is ``(chapter_index, scene_index)`` and the id is
    ``c{chapter}_s{scene}`` — the same scene id ``docs/STUDIO-API.md`` puts on
    ``Scene`` and ``PassScene``, so the UI, the pass and the script all agree.

    Lines inside a paragraph are joined with a single space and paragraphs are
    separated by a blank line, because that is what the text looked like before
    ``build.py`` split it into sentences. Handing the model sentence-per-line
    text makes it annotate each line as if it stood alone, which is the drift
    this whole scene-level design exists to avoid.
    """
    titles = {c.index: c.title for c in script.chapters}
    ordered = sorted(script.utterances, key=lambda u: u.order)

    grouped: dict[tuple[int, int], list] = {}
    for u in ordered:
        grouped.setdefault((u.chapter_index, u.scene_index), []).append(u)

    passages: list[ScenePassage] = []
    for (ci, si), utterances in grouped.items():
        lines = [
            PassageLine(
                id=u.id,
                text=u.text,
                speaker=u.speaker,
                kind=getattr(u.kind, "value", str(u.kind)),
                paragraph=_paragraph_of(u.id),
            )
            for u in utterances
        ]
        paragraphs: list[list[str]] = []
        previous_key: str | None = None
        # ``text`` is built from the directed lines only (see SKIPPED_KINDS);
        # ``lines`` keeps the whole scene so the UI's rail still covers it.
        for line in [l for l in lines if l.kind not in SKIPPED_KINDS]:
            if previous_key is None or line.paragraph != previous_key:
                paragraphs.append([])
            paragraphs[-1].append(line.text)
            previous_key = line.paragraph
        text = "\n\n".join(" ".join(p) for p in paragraphs)

        passages.append(
            ScenePassage(
                scene_id=f"c{ci}_s{si}",
                chapter_index=ci,
                scene_index=si,
                chapter_title=titles.get(ci, ""),
                text=text,
                lines=lines,
            )
        )
    return passages


# ---- tag distribution -------------------------------------------------------

def distribute_tags(passage: ScenePassage, annotated: str, allowed_tags: Sequence[str]) -> dict[str, str]:
    """Walk an annotated scene back onto its individual lines.

    The gateway validated the *scene*: strip the tags and what remains is the
    passage. The renderer needs per-utterance text, so this splits the annotated
    string at the line boundaries and re-checks every line on its own.

    Placement rule: a tag inside a line's span stays where it is; a tag sitting
    in the gap between two lines attaches to the **following** line (the prompt
    asks for tags "before the words it colours"), except tags after the last
    line's final word, which attach to the last line because there is nothing
    after them.

    Raises ``TagDistributionError`` rather than guessing. A tag attached to the
    wrong utterance is a wrong take that sounds perfectly fine — exactly the
    class of failure that has to fail loudly.
    """
    pattern = _tag_pattern(allowed_tags)
    if pattern is None:
        raise TagDistributionError("the target engine declares no audio tags")

    spans = [(m.start(), m.end()) for m in pattern.finditer(annotated)]
    is_tag = bytearray(len(annotated))
    for start, end in spans:
        for i in range(start, end):
            is_tag[i] = 1

    # Every significant (non-tag, non-whitespace) character, with its position.
    stream = [(ch, i) for i, ch in enumerate(annotated) if not is_tag[i] and not ch.isspace()]

    cursor = 0
    lines = passage.directed_lines  # exactly the lines ``passage.text`` was built from
    line_spans: list[tuple[int, int] | None] = []
    for line in lines:
        wanted = [c for c in line.text if not c.isspace()]
        if not wanted:
            line_spans.append(None)
            continue
        chunk = stream[cursor : cursor + len(wanted)]
        if len(chunk) < len(wanted) or [c for c, _ in chunk] != wanted:
            raise TagDistributionError(
                f"line {line.id} does not line up with the annotated scene"
            )
        line_spans.append((chunk[0][1], chunk[-1][1] + 1))
        cursor += len(wanted)

    if cursor != len(stream):
        leftover = "".join(c for c, _ in stream[cursor:])[:40]
        raise TagDistributionError(
            f"the annotated scene has {len(stream) - cursor} extra characters after the "
            f"last line (…{leftover})"
        )

    placed = [i for i, span in enumerate(line_spans) if span is not None]
    spoken: dict[str, str] = {}
    previous_end = 0
    for n, idx in enumerate(placed):
        start, end = line_spans[idx]  # type: ignore[misc]
        # Start at the previous line's end so any tag in the gap comes with us;
        # the last line also swallows whatever trails the passage.
        lo = previous_end
        hi = len(annotated) if n == len(placed) - 1 else end
        text = " ".join(annotated[lo:hi].split())
        line = lines[idx]
        if _squash(pattern.sub(" ", text)) != _squash(line.text):
            raise TagDistributionError(
                f"stripping tags from line {line.id} did not reproduce its text"
            )
        spoken[line.id] = text
        previous_end = end
    return spoken


def _count_tags(text: str, allowed_tags: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for tag in allowed_tags:
        n = text.count(tag)
        if n:
            counts[tag] = n
    return counts


# ---- pass documents ---------------------------------------------------------

class PassCounts(BaseModel):
    kept: int = 0
    rejected: int = 0
    ignored: int = 0
    error: int = 0
    scenes: int = 0
    """Scenes actually directed. Always ``kept + rejected + ignored + error``; a
    pass stopped early has fewer than ``scope.scenes``."""

    # The studio sends a pass as stored: see ``schema.WIRE_SCHEMA``.
    model_config = WIRE_SCHEMA


class PassScene(BaseModel):
    """One scene's proposal. Mirrors ``PassScene`` in docs/STUDIO-API.md."""

    scene_id: str
    chapter_index: int = 0
    scene_index: int = 0
    chapter_title: str = ""
    status: Literal["kept", "rejected", "ignored", "error"] = "ignored"
    instruction: str | None = None
    tags_status: Literal["kept", "rejected", "none"] = "none"
    tags_used: dict[str, int] = Field(default_factory=dict)
    source_text: str = ""
    model_text: str | None = None
    """The model's annotated passage, kept OR rejected — the review screen diffs
    it against ``source_text``. A rejection with the text thrown away is not
    reviewable, which at book scale means not fixable."""
    line_spoken_text: dict[str, str] | None = None
    summary: str = ""
    emotion: str = "neutral"
    scalar: float = Field(default=0.5, ge=0.0, le=1.0)
    scene_note: str | None = None
    """Context for the human ("an exchange between Elara and Marcus…"), shown in
    the Desk and **never sent to an engine**. Kept apart from ``instruction``
    because a scene note and a line note are different documents: the note that
    goes to a synthesis engine is a delivery instruction for *that* line and
    *that* speaker."""
    pace: float | None = None
    """Relative reading speed the director asked for, 0.8-1.2. Unlike
    ``instruction`` this is performable on **every** engine, so it is what makes
    an accepted pass audible on a Kokoro-only book."""
    decision: Literal["pending", "accepted", "edited", "rejected", "kept_text"] = "pending"
    final_instruction: str | None = None
    attempts: int = 0
    error: str | None = None
    reason: str | None = None
    """Why a scene was rejected or ignored, in one sentence. In the contract since
    Amendment v1.4 of docs/STUDIO-API.md: ``error`` is reserved for a failed call,
    and "rejected" with no reason is unactionable across four hundred scenes."""
    prompt_version: str = ""
    """The prompt that produced *this* scene (contract Amendment v1.4), and
    load-bearing for a resumed pass: the prompt is a file anyone can edit mid-run, so a pass can
    legitimately contain scenes from two prompts and the review screen should be
    able to say so."""

    model_config = WIRE_SCHEMA


class PassScope(BaseModel):
    scenes: list[str] = Field(default_factory=list)

    model_config = WIRE_SCHEMA


class Pass(BaseModel):
    """A director pass over a book. Persisted by the studio as passes/<id>.json."""

    id: str
    book: str = ""
    created_at: str = ""
    finished_at: str | None = None
    """Set only when the whole scope completed — a pass stopped by ``should_stop``
    leaves it null so the studio can resume it."""
    provider: Literal["ollama", "gemini"] = "ollama"
    model: str = ""
    prompt_version: str = ""
    target_engine: str = ""
    scope: PassScope = Field(default_factory=PassScope)
    counts: PassCounts = Field(default_factory=PassCounts)
    elapsed_s: float = 0.0
    scenes: list[PassScene] = Field(default_factory=list)
    applied_at: str | None = None

    model_config = WIRE_SCHEMA

    def scene(self, scene_id: str) -> PassScene | None:
        for s in self.scenes:
            if s.scene_id == scene_id:
                return s
        return None


def recount(pass_: Pass) -> PassCounts:
    """Recompute ``counts`` from ``scenes``. Call after any edit to a pass."""
    counts = PassCounts(scenes=len(pass_.scenes))
    for scene in pass_.scenes:
        setattr(counts, scene.status, getattr(counts, scene.status) + 1)
    pass_.counts = counts
    return counts


# ---- directing one scene ----------------------------------------------------

def _normalize_spec(spec: EngineSpecDict) -> EngineSpecDict:
    """Fill in the fields the gateway's DirectorEngineSpec expects.

    ``instruction`` defaults to True because a director pass exists to write
    performance notes — but an explicit ``False`` is respected, so a tags-only
    engine can still be targeted.
    """
    out = dict(spec)
    out.setdefault("name", out.get("id", ""))
    out.setdefault("emotion_labels", [])
    out.setdefault("voices", [])
    out.setdefault("audio_tags", [])
    out.setdefault("instruction", True)
    return out


def _entry_for(raw: dict, engine_id: str) -> dict:
    for item in raw.get("per_engine") or []:
        if isinstance(item, dict) and item.get("engine_id") == engine_id:
            return item
    return {}


def direct_scene(
    passage: ScenePassage,
    annotate: AnnotateFn,
    target_spec: EngineSpecDict,
    prompt_version: str = "",
) -> PassScene:
    """Direct one scene and return its proposal. Never raises.

    Status, per docs/STUDIO-API.md:

    - ``rejected`` — the annotated text was not the book (or would not
      distribute onto the lines). The note, if any, is still carried so the
      reviewer can take ``kept_text``: keep the direction, keep the words.
    - ``kept``     — an instruction and/or a usable tag annotation survived.
    - ``ignored``  — nothing usable came back for this engine.
    - ``error``    — the call failed; the message is in ``error`` and the pass
      keeps going. One unreachable moment must not lose 300 directed scenes.
    """
    spec = _normalize_spec(target_spec)
    engine_id = str(spec.get("id", ""))
    allowed_tags = list(spec.get("audio_tags") or [])

    scene = PassScene(
        scene_id=passage.scene_id,
        chapter_index=passage.chapter_index,
        scene_index=passage.scene_index,
        chapter_title=passage.chapter_title,
        source_text=passage.text,
        attempts=1,
        prompt_version=prompt_version,
    )

    try:
        raw = annotate(passage.text, [spec])
    except Exception as exc:  # noqa: BLE001 - a pass must survive one bad scene
        scene.status = "error"
        scene.error = f"{type(exc).__name__}: {exc}"
        return scene
    if not isinstance(raw, dict):
        scene.status = "error"
        scene.error = f"director returned {type(raw).__name__}, expected a JSON object"
        return scene

    scene.summary = str(raw.get("summary") or "")
    scene.emotion = str(raw.get("emotion") or "neutral")
    try:
        scene.scalar = max(0.0, min(1.0, float(raw.get("scalar", 0.5))))
    except (TypeError, ValueError):
        scene.scalar = 0.5
    scene.scene_note = _clean_instruction(raw.get("scene_note"))
    scene.pace = clamp_pace(raw.get("pace"))
    if raw.get("prompt_version"):
        scene.prompt_version = str(raw["prompt_version"])

    entry = _entry_for(raw, engine_id)
    scene.instruction = _clean_instruction(entry.get("instruction"))

    # The gateway already ruled on the text. Derive the verdict for an older
    # gateway that predates text_status rather than guessing "kept".
    status = entry.get("text_status")
    if status not in ("kept", "rejected", "none"):
        status = "kept" if entry.get("text") else ("rejected" if entry.get("rejected_text") else "none")

    if status == "kept" and entry.get("text"):
        annotated = str(entry["text"])
        scene.model_text = annotated
        try:
            scene.line_spoken_text = distribute_tags(passage, annotated, allowed_tags)
        except TagDistributionError as exc:
            # Validated as a scene, unusable as lines. Reject the whole scene:
            # a tag on the wrong utterance is a wrong take, silently.
            scene.line_spoken_text = None
            scene.tags_status = "rejected"
            scene.status = "rejected"
            scene.reason = f"tags could not be mapped onto the lines: {exc}"
            return scene
        scene.tags_status = "kept"
        scene.tags_used = _count_tags(annotated, allowed_tags)
    elif status == "rejected":
        scene.tags_status = "rejected"
        scene.model_text = entry.get("rejected_text") or entry.get("text")
        scene.status = "rejected"
        scene.reason = entry.get("text_reason") or "the annotated text was not the source passage"
        return scene
    else:
        scene.tags_status = "none"
        scene.reason = entry.get("text_reason") or None

    if scene.instruction or scene.tags_status == "kept":
        scene.status = "kept"
    else:
        scene.status = "ignored"
        scene.reason = scene.reason or "the director returned nothing usable for this engine"
    return scene


# ---- cache ------------------------------------------------------------------

def scene_cache_key(
    passage_text: str,
    model: str,
    prompt_version: str,
    engine_id: str,
    audio_tags: Sequence[str],
) -> str:
    """Content hash of everything that can change a scene's direction.

    Same discipline as the render cache: an unchanged scene costs nothing on a
    re-run, and anything that *would* change the answer — the words, the model,
    the prompt, which engine the note is written for, that engine's tag
    vocabulary — is in the key. The tag list matters because pinning
    ``BREEZE_AUDIO_TAGS`` to a smaller set genuinely changes what the director
    may insert.
    """
    h = hashlib.sha256()
    h.update(
        "\x00".join(
            [passage_text, model, prompt_version, engine_id, ",".join(sorted(set(audio_tags)))]
        ).encode("utf-8")
    )
    return h.hexdigest()[:32]


# ---- running a pass ---------------------------------------------------------

def new_pass_id() -> str:
    return f"direct-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def run_pass(
    script: BookScript,
    *,
    scenes: list[str] | None = None,
    annotate: AnnotateFn,
    target_spec: EngineSpecDict,
    provider: str = "ollama",
    model: str = "",
    prompt_version: str = "",
    pass_id: str | None = None,
    book: str = "",
    checkpoint: Callable[[Pass], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int, PassScene], None] | None = None,
    cache: dict | None = None,
) -> Pass:
    """Direct every scene (or ``scenes``) of ``script`` and return the pass.

    - ``checkpoint`` is called with the pass after **every** scene, so the studio
      can persist it. A crash at scene 380 of 400 loses one scene, not 380.
    - ``should_stop`` is polled before each scene (cooperative pause/cancel). A
      stopped pass returns with the remaining scenes simply **absent** and
      ``finished_at`` null — a partial document, not a broken one.
    - ``cache`` maps ``scene_cache_key`` -> the raw annotate reply. Re-running
      after editing two scenes costs two calls. Pass the *same* dict across runs
      (the studio keeps it per book).

    Unknown ids in ``scenes`` are skipped rather than raising: a stale scene id
    from a UI that is one script-version behind must not kill the job.
    """
    spec = _normalize_spec(target_spec)
    engine_id = str(spec.get("id", ""))
    allowed_tags = list(spec.get("audio_tags") or [])

    passages = scene_passages(script)
    if scenes is not None:
        wanted = set(scenes)
        passages = [p for p in passages if p.scene_id in wanted]
    else:
        # Default scope = the scenes that will actually be heard. A chapter
        # marked ``include_in_audio: false`` is never synthesized, so directing
        # it spends a model call (and, at book scale, a GPU-minute) on a
        # copyright page: 2 of the 7 scenes in a recorded stub-gateway trace
        # were the front matter of hard-manuscript.txt. An explicit ``scenes``
        # list still wins — naming a scene is asking for it.
        audible = {c.index for c in script.chapters if getattr(c, "include_in_audio", True)}
        passages = [p for p in passages if p.chapter_index in audible]

    pass_ = Pass(
        id=pass_id or new_pass_id(),
        book=book,
        created_at=_now(),
        provider=provider if provider in ("ollama", "gemini") else "ollama",
        model=model,
        prompt_version=prompt_version,
        target_engine=engine_id,
        scope=PassScope(scenes=[p.scene_id for p in passages]),
    )
    recount(pass_)

    def cached_annotate(text: str, specs: list[EngineSpecDict]) -> dict:
        if cache is None:
            return annotate(text, specs)
        key = scene_cache_key(text, model, prompt_version, engine_id, allowed_tags)
        if key in cache:
            return cache[key]
        result = annotate(text, specs)
        cache[key] = result
        return result

    total = len(passages)
    started = time.monotonic()
    for index, passage in enumerate(passages):
        if should_stop is not None and should_stop():
            break
        scene = direct_scene(passage, cached_annotate, spec, prompt_version)
        pass_.scenes.append(scene)
        recount(pass_)
        pass_.elapsed_s = round(time.monotonic() - started, 3)
        if checkpoint is not None:
            checkpoint(pass_)
        if on_progress is not None:
            on_progress(index + 1, total, scene)
    else:
        pass_.finished_at = _now()

    pass_.elapsed_s = round(time.monotonic() - started, 3)
    return pass_


def retry_scenes(
    pass_: Pass,
    script: BookScript,
    scene_ids: Sequence[str],
    annotate: AnnotateFn,
    *,
    target_spec: EngineSpecDict | None = None,
    prompt_version: str | None = None,
    cache: dict | None = None,
    checkpoint: Callable[[Pass], None] | None = None,
    on_progress: Callable[[int, int, PassScene], None] | None = None,
) -> Pass:
    """Re-run the director for ``scene_ids`` **in place**, in the same pass.

    ``attempts`` carries over and increments, and the decision resets to
    ``pending`` — the reviewer asked for another take, so the old decision (and
    the old note) must not survive as if it had been re-approved.

    A retry deliberately bypasses ``cache`` and *invalidates* the entry: asking
    again is the entire point, and a later ``run_pass`` must not resurrect the
    reply the reviewer just rejected.
    """
    spec = _normalize_spec(target_spec or DEFAULT_SPECS.get(pass_.target_engine) or {"id": pass_.target_engine})
    version = pass_.prompt_version if prompt_version is None else prompt_version
    by_scene = {p.scene_id: p for p in scene_passages(script)}

    wanted = [sid for sid in scene_ids]
    total = len(wanted)
    done = 0
    for index, old in enumerate(pass_.scenes):
        if old.scene_id not in wanted:
            continue
        passage = by_scene.get(old.scene_id)
        if passage is None:
            continue
        if cache is not None:
            cache.pop(
                scene_cache_key(
                    passage.text,
                    pass_.model,
                    version,
                    str(spec.get("id", "")),
                    list(spec.get("audio_tags") or []),
                ),
                None,
            )
        fresh = direct_scene(passage, annotate, spec, version)
        fresh.attempts = old.attempts + 1
        fresh.decision = "pending"
        pass_.scenes[index] = fresh
        done += 1
        recount(pass_)
        if checkpoint is not None:
            checkpoint(pass_)
        if on_progress is not None:
            on_progress(done, total, fresh)
    return pass_


# ---- applying a pass to the script -----------------------------------------

_APPLIED_DECISIONS = ("accepted", "edited", "kept_text")


def apply_pass(
    script: BookScript,
    pass_: Pass,
    *,
    instruction_engines: set[str] | None = None,
    force: bool = False,
    applied_at: str | None = None,
) -> list[str]:
    """Write the decided scenes into ``script``. Returns the ids of lines changed.

    - Only ``accepted`` / ``edited`` / ``kept_text`` scenes are applied.
      ``pending`` and ``rejected`` leave the book untouched.
    - **``intent`` is written on every line of an accepted scene, whatever engine
      it is routed to.** ``intent`` is the engine-agnostic layer the whole
      architecture is justified by, and it was a constant: 100% neutral/0.5 in
      every book, including one that had been through a complete director pass.
      Emotion and intensity come from the scene; ``intent.pace`` records what the
      director asked for.
    - **A pace becomes real speed on engines that take no written note.** For
      those lines ``render.speed = profile.base_speed × pace`` (clamped
      0.7-1.3), so an accepted pass is audible on Kokoro instead of being
      discarded. Idempotent by construction: the multiplier is always applied to
      the *base*, recovered from the profile (or by dividing out the pace already
      stored in ``intent.pace``), so applying twice never compounds.
    - Only lines routed to an **instruction-capable** engine are stamped with the
      note (``instruction_engines``, default ``{pass_.target_engine}``). A note on
      a Kokoro line does nothing audible but still changes its cache key, so it
      would silently re-render the narrator for no reason —
      ``instruction_is_inert`` is the same rule, exposed so the studio can warn
      about a note a person types by hand.
    - ``scene_note`` is never written to a line. It is context for the human.
    - A line a person edited (``direction_source == "manual"``) is **never**
      overwritten unless ``force=True`` — not its note, and not its intent or
      speed either. That is semantics rule 4 in docs/STUDIO-API.md, and the
      reason ``direction_source`` exists at all: "hands off this line" has to
      mean the whole line, or a pass could still change what it sounds like.
    - ``kept_text`` applies the note and clears ``spoken_text``: keep the
      direction, keep the author's words.

    Idempotent: applying twice leaves the same script and reports no second
    round of changes.
    """
    targets = set(instruction_engines) if instruction_engines else {pass_.target_engine}
    by_scene = {p.scene_id: p for p in scene_passages(script)}
    utterances = {u.id: u for u in script.utterances}
    profile_speed = script.profile.base_speed if script.profile is not None else None
    changed: list[str] = []

    for scene in pass_.scenes:
        if scene.decision not in _APPLIED_DECISIONS:
            continue
        passage = by_scene.get(scene.scene_id)
        if passage is None:
            continue  # the script moved on; nothing to stamp

        final = scene.final_instruction or scene.instruction
        scene.final_instruction = final
        source = "manual" if scene.decision == "edited" else "director"
        use_tags = (
            scene.decision != "kept_text"
            and scene.tags_status == "kept"
            and bool(scene.line_spoken_text)
        )

        # Only the lines the director was actually shown (SKIPPED_KINDS): a
        # chapter announcement is not performed by the scene it introduces.
        for line_id in passage.directed_line_ids:
            u = utterances.get(line_id)
            if u is None:
                continue
            if u.render.direction_source == "manual" and not force:
                continue  # a line a person edited is left alone entirely

            # Engine-agnostic first: what the passage *feels* like is true of the
            # line whatever synthesizes it, so this runs before the engine gate.
            intent_before = (u.intent.emotion, u.intent.intensity, u.intent.pace)
            u.intent.emotion = scene.emotion or "neutral"
            u.intent.intensity = scene.scalar
            if scene.pace is not None:
                u.intent.pace = scene.pace
            speed_before = u.render.speed
            if scene.pace is not None and instruction_is_inert(u.render.engine, targets):
                base = _base_speed(u, profile_speed, intent_before[2])
                u.render.speed = round(
                    max(MIN_SPEED, min(MAX_SPEED, base * scene.pace)), 3
                )
            if (
                (u.intent.emotion, u.intent.intensity, u.intent.pace) != intent_before
                or u.render.speed != speed_before
            ) and line_id not in changed:
                changed.append(line_id)

            if u.render.engine not in targets:
                continue

            before = (u.render.instruction, u.render.spoken_text, u.render.direction_source)
            spoken = (scene.line_spoken_text or {}).get(line_id) if use_tags else None
            # Storing a "spoken text" identical to the text would change nothing
            # audible while inventing a different cache key for it.
            if spoken is not None and _squash(spoken) == _squash(u.text):
                spoken = None

            u.render.instruction = final
            u.render.spoken_text = spoken
            u.render.direction_source = source
            if (
                u.render.instruction,
                u.render.spoken_text,
                u.render.direction_source,
            ) != before and line_id not in changed:
                changed.append(line_id)

    pass_.applied_at = applied_at or _now()
    return changed


def _base_speed(u, profile_speed: float | None, previous_pace: float) -> float:
    """The speed this line reads at with *no* direction — the multiplicand.

    The profile's ``base_speed`` when the script carries a profile (it always
    does when built by ``build.py``). Otherwise the line's own current speed,
    with any pace a previous apply already multiplied in divided back out. That
    is what keeps re-applying a pass from compounding 0.92 into 0.85 into 0.78.
    """
    if profile_speed:
        return profile_speed
    if previous_pace and previous_pace != 1.0:
        return round(u.render.speed / previous_pace, 6)
    return u.render.speed


# ---- annotate backends ------------------------------------------------------

def http_annotate(
    gateway_url: str = DEFAULT_GATEWAY,
    provider: str = "ollama",
    model: str = "",
    api_key: str | None = None,
    *,
    timeout: float = 600.0,
    ollama_url: str | None = None,
) -> AnnotateFn:
    """Call the running gateway's director. stdlib ``urllib``, like ``http_synth``.

    Validation (the anti-paraphrase rule) happens on the gateway side — this is
    the reason the pipeline never re-implements it.
    """

    def annotate(text: str, specs: list[EngineSpecDict]) -> dict:
        payload: dict[str, Any] = {
            "text": text,
            "provider": provider,
            "model": model,
            "engines": specs,
        }
        if api_key:
            payload["api_key"] = api_key
        if ollama_url:
            payload["ollama_url"] = ollama_url
        req = urllib.request.Request(
            f"{gateway_url.rstrip('/')}/api/director/annotate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    return annotate


# A one-word swap per paraphrased scene: plausible enough to be the real failure
# mode (an LLM "improving" the prose), small enough that the rejection is clearly
# about identity and not about garbage.
_FAKE_SWAPS = {
    "said": "replied",
    "was": "seemed",
    "were": "seemed",
    "the": "that",
    "and": "but",
    "had": "held",
    "very": "deeply",
    "with": "beside",
    "into": "toward",
    "down": "aside",
}


def _fake_paraphrase(text: str) -> str | None:
    """Swap exactly one word. Returns None if nothing could be swapped."""
    for match in re.finditer(r"[A-Za-z']+", text):
        word = match.group(0)
        swap = _FAKE_SWAPS.get(word.lower())
        if swap:
            if word[0].isupper():
                swap = swap.capitalize()
            return text[: match.start()] + swap + text[match.end() :]
    for match in re.finditer(r"[A-Za-z']{6,}", text):
        return text[: match.start()] + "somehow" + text[match.end() :]
    return None


_SENTENCE_END = re.compile(r"[.!?][\"'”’)\]]*\s")


def _fake_insert_tag(text: str, tag: str) -> str:
    """Put ``tag`` after the first sentence, or in front if there isn't one."""
    match = _SENTENCE_END.search(text)
    if match:
        return f"{text[: match.end()]}{tag} {text[match.end() :]}"
    return f"{tag} {text}"


def _fake_scene_note(passage: ScenePassage | None, text: str) -> str:
    """Context for the human: who is in the scene and what it is doing.

    This is the text that used to be stamped verbatim on every line as the
    *engine's* instruction — naming characters who are not speaking that line.
    It stays exactly as scene-shaped as it was; it just no longer goes anywhere
    near an engine.
    """
    if passage is not None and passage.lines:
        speakers = list(dict.fromkeys(line.speaker for line in passage.lines))
        spoken = [line for line in passage.lines if line.kind in ("dialogue", "internal")]
        share = len(spoken) / len(passage.lines)
        others = [s for s in speakers if s != "Narrator"]
        if len(others) >= 2 and share > 0.3:
            return (
                f"An exchange between {others[0]} and {others[1]}: the turns are close, and the "
                f"room stays quiet underneath them."
            )
        if others and share > 0.15:
            return f"{others[0]} speaks into a quiet room; nobody answers for a beat."
        return "Narration only — no one speaks aloud in this scene."
    if '"' in text or "“" in text:
        return "Dialogue-led: short turns, little narration between them."
    return "Narration only — no one speaks aloud in this scene."


def _fake_note(passage: ScenePassage | None, text: str) -> str:
    """A plausible **line** note: how to say this line, to whoever says it.

    Deterministic, derived from the shape of the scene, and deliberately free of
    other characters' names — an engine voicing four words of Elara must not be
    told about Marcus.
    """
    tense = "?" in text or "!" in text
    if passage is not None and passage.lines:
        spoken = [line for line in passage.lines if line.kind in ("dialogue", "internal")]
        share = len(spoken) / len(passage.lines)
        if share > 0.3:
            return (
                "Clipped and close, half a beat before you start, and let the last word land "
                "without lifting."
                if tense
                else "Keep it low and even, a half-beat before you start, no lift at the end."
            )
        if share > 0.15:
            return "Low and unhurried, holding the tension on the last word."
        return (
            "Steady and unhurried, letting each clause settle before the next, with no lift at "
            "the end of the sentence."
        )
    if '"' in text or "“" in text:
        return "Clipped and close, a half-beat before you start, and no lift at the end."
    return "Steady and unhurried, letting each clause settle before the next."


def _fake_pace(text: str) -> float:
    """0.92 for a scene with a question or an exclamation in it, else 1.0.

    Crude on purpose: the fake exists so the *plumbing* (clamp → intent.pace →
    render.speed on a non-instruction engine) is exercised with no model on the
    box. It varies, which is the property the tests need."""
    return 0.92 if ("?" in text or "!" in text) else 1.0


def fake_annotate(
    script: BookScript | None = None,
    *,
    paraphrase_every: int = 5,
) -> AnnotateFn:
    """Deterministic offline director (``STUDIO_FAKE_DIRECTOR=1``). No network.

    It emulates the gateway *after* validation — i.e. it returns ``text`` /
    ``text_status`` / ``rejected_text`` exactly as the real endpoint would — so
    the studio, the review screen and this module run the same code paths with
    no model on the box (semantics rule 6 in docs/STUDIO-API.md).

    - the line note comes from the scene's speakers and kinds (hence the optional
      ``script``: ``AnnotateFn`` only carries text, so the passage is looked up
      by its text to recover who is in it), and the scene note — the one that
      names the characters — is returned separately, as the real director now
      does,
    - a ``pace`` comes back too (0.92 for a scene with a question or an
      exclamation in it, 1.0 otherwise), so the "pace reaches ``render.speed`` on
      an engine that takes no written note" path is exercised offline,
    - the target engine's **first** allowed tag is inserted after the first
      sentence,
    - **every ``paraphrase_every``-th scene** (the 1st, 6th, 11th … the fake is
      asked about — 0-based, so a short book still exercises it) comes back
      paraphrased by one word and therefore *rejected*, with the instruction
      intact. That is the ``kept_text`` review case, and it is the path most
      likely to be wrong, so it must not need a GPU to reach.

    Deterministic per call sequence. Scene results that matter across runs are
    held by the pass cache, which is keyed on the passage text, not on order.
    """
    index = {p.text: p for p in scene_passages(script)} if script is not None else {}
    state = {"n": 0}

    def annotate(text: str, specs: list[EngineSpecDict]) -> dict:
        n = state["n"]
        state["n"] = n + 1
        passage = index.get(text)
        spec = _normalize_spec(specs[0]) if specs else {"id": "", "audio_tags": [], "instruction": True}
        tags = list(spec.get("audio_tags") or [])
        note = _fake_note(passage, text) if spec.get("instruction") else None

        entry: dict[str, Any] = {
            "engine_id": spec.get("id", ""),
            "emotion_label": None,
            "voice": None,
            "scalar": 0.5,
            "instruction": note,
            "text": None,
            "rejected_text": None,
            "text_status": "none",
            "text_reason": None if tags else "engine declares no audio tags",
        }

        if tags:
            paraphrased = (
                _fake_paraphrase(text) if paraphrase_every > 0 and n % paraphrase_every == 0 else None
            )
            if paraphrased is not None:
                entry["rejected_text"] = _fake_insert_tag(paraphrased, tags[0])
                entry["text_status"] = "rejected"
                entry["text_reason"] = (
                    f"{FAKE_MODEL}: one word was swapped, so this is not the source passage"
                )
            else:
                entry["text"] = _fake_insert_tag(text, tags[0])
                entry["text_status"] = "kept"

        return {
            "summary": _fake_summary(passage, text),
            "emotion": "sad" if ("?" in text or "!" in text) else "calm",
            "scalar": 0.55,
            "voice_hint": "a warm, unhurried adult narrator",
            "scene_note": _fake_scene_note(passage, text),
            "pace": _fake_pace(text),
            "segments": [],
            "per_engine": [entry],
            "prompt_version": FAKE_PROMPT_VERSION,
        }

    return annotate


def _fake_summary(passage: ScenePassage | None, text: str) -> str:
    words = len(text.split())
    if passage is not None:
        others = [s for s in dict.fromkeys(l.speaker for l in passage.lines) if s != "Narrator"]
        who = ", ".join(others[:3]) if others else "the narrator alone"
        return f"{FAKE_MODEL}: {len(passage.lines)} lines, {words} words, voices: {who}."
    return f"{FAKE_MODEL}: {words} words."


# ---- engine specs -----------------------------------------------------------

def engine_spec_from_gateway(
    gateway_url: str, engine_id: str, *, timeout: float = 15.0
) -> EngineSpecDict:
    """Fetch one engine's real spec from a running gateway (``GET /api/engines``).

    Preferred over ``DEFAULT_SPECS``: the tag vocabulary is engine-side config
    (``BREEZE_AUDIO_TAGS``), so a hardcoded copy here would drift and the
    director would be told about tags the engine no longer honours.
    """
    req = urllib.request.Request(f"{gateway_url.rstrip('/')}/api/engines", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        entries = json.loads(resp.read().decode("utf-8"))
    for entry in entries:
        info = entry.get("info") or {}
        if info.get("id") != engine_id:
            continue
        caps = info.get("capabilities") or {}
        return {
            "id": info["id"],
            "name": info.get("name", engine_id),
            "emotion_labels": info.get("emotion_labels", []),
            "voices": [v["id"] for v in info.get("voices", [])],
            "audio_tags": info.get("audio_tags", []),
            "emotion_scalar": caps.get("emotion_scalar", False),
            "emotion_tags": caps.get("emotion_tags", False),
            "instruction": caps.get("instruction", False),
        }
    raise ValueError(f"engine '{engine_id}' is not present at {gateway_url}/api/engines")


def resolve_spec(engine_id: str, gateway_url: str | None = None) -> EngineSpecDict:
    """The live spec if a gateway is reachable, else the offline default."""
    if gateway_url:
        try:
            return engine_spec_from_gateway(gateway_url, engine_id)
        except Exception as exc:  # noqa: BLE001 - fall back, but say so
            print(f"  ! could not read {engine_id} from {gateway_url} ({exc}); using built-in spec")
    spec = DEFAULT_SPECS.get(engine_id)
    if spec is None:
        raise SystemExit(
            f"no built-in spec for engine '{engine_id}'. Pass --gateway so the real one can be "
            f"fetched, or add it to DEFAULT_SPECS."
        )
    return dict(spec)


# ---- CLI --------------------------------------------------------------------

def default_target_engine(script: BookScript, capable: Sequence[str] | set[str]) -> str:
    """The instruction-capable engine **most lines in this book actually use**.

    ``""`` when the book has no line on any of them, which is the honest answer:
    a pass written for Breeze on a Kokoro-only book stamps nothing and says
    nothing about having stamped nothing. Mirrors
    ``studio.store.default_target_engine`` (the studio resolves this from the
    live catalog; the CLI has only the built-in specs, so the rule is restated
    rather than imported — same container boundary as ``_squash``).
    """
    capable = set(capable)
    counts: dict[str, int] = {}
    for u in script.utterances:
        if u.render.engine in capable:
            counts[u.render.engine] = counts.get(u.render.engine, 0) + 1
    if not counts:
        return ""
    return max(sorted(counts), key=lambda engine: counts[engine])


def _load_script(
    path: str, book_type: str, engine: str, casting_mode: str = "narrator_only"
) -> BookScript:
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as fh:
            return BookScript.model_validate_json(fh.read())
    from .build import build_book_script

    return build_book_script(
        path, book_type=book_type, engine=engine, casting_mode=casting_mode
    )


def _print_summary(pass_: Pass) -> None:
    counts = pass_.counts
    print(f"\n{'=' * 66}")
    print(f"pass       {pass_.id}")
    print(f"director   {pass_.provider}:{pass_.model}  prompt {pass_.prompt_version or '(unknown)'}")
    print(f"target     {pass_.target_engine}")
    print(f"scenes     {counts.scenes} of {len(pass_.scope.scenes)} in scope   {pass_.elapsed_s:.2f}s")
    print(f"\n  KEPT     {counts.kept:4d}  an instruction and/or tags survived validation")
    print(f"  REJECTED {counts.rejected:4d}  the model's text was not the book (review the diff)")
    print(f"  IGNORED  {counts.ignored:4d}  nothing usable for this engine")
    print(f"  ERROR    {counts.error:4d}  the call failed")

    # The engine-agnostic half of the pass. `pace` lands on every line of an
    # accepted scene (and on render.speed for engines that take no note), while
    # `scene_note` is for the person reading this and never reaches an engine.
    print("\n  scene        pace  emo            note (to the performer) / scene note (to you)")
    for scene in pass_.scenes:
        pace = f"{scene.pace:.2f}" if scene.pace is not None else "  — "
        print(f"  {scene.scene_id:<12} {pace}  {scene.emotion:<8} {scene.scalar:.2f}")
        if scene.instruction:
            print(f"       line     {scene.instruction[:96]}")
        if scene.scene_note:
            print(f"       scene    {scene.scene_note[:96]}")

    for scene in pass_.scenes:
        if scene.status == "kept":
            continue
        head = f"  {scene.status.upper():<8} {scene.scene_id} (ch{scene.chapter_index})"
        print(f"\n{head}")
        if scene.reason or scene.error:
            print(f"           why   {scene.error or scene.reason}")
        if scene.model_text:
            print(f"           got   {scene.model_text[:140]}")
            print(f"           book  {scene.source_text[:140]}")
        if scene.instruction:
            print(f"           note  {scene.instruction[:140]}  (usable via kept_text)")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # a Windows console defaults to cp1252; directed prose is full of curly quotes
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        description="Run a book-scale, scene-level director pass.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("path", help="a Book Script .json, or a .txt/.md book to build one from")
    ap.add_argument("--out", required=True, help="where to write the pass JSON")
    ap.add_argument("--fake", action="store_true", help="offline canned director (no LLM, no network)")
    ap.add_argument("--gateway", default=os.environ.get("AUDIOBOOK_GATEWAY", DEFAULT_GATEWAY))
    ap.add_argument("--model", default=os.environ.get("DIRECTOR_MODEL", "gemma3:12b"))
    ap.add_argument("--provider", choices=["ollama", "gemini"], default="ollama")
    ap.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY", ""))
    ap.add_argument(
        "--target-engine",
        default="auto",
        help="engine the notes are written for; 'auto' = the instruction-capable "
        "engine most lines of this book are already routed to",
    )
    ap.add_argument(
        "--build-engine",
        default="kokoro",
        help="narrator engine when building a script from a .txt (NOT the target engine)",
    )
    ap.add_argument(
        "--casting-mode",
        default="narrator_only",
        help="only used when building from a .txt: narrator_only | dialogue_expressive | all_expressive",
    )
    ap.add_argument("--scenes", default="", help="comma-separated scene ids (default: all)")
    ap.add_argument("--book-type", default="auto", help="only used when building from a .txt")
    ap.add_argument("--apply", action="store_true", help="accept every KEPT scene and stamp the script")
    ap.add_argument("--script-out", default=None, help="where to write the script after --apply")
    args = ap.parse_args(argv)

    fake = args.fake or os.environ.get("STUDIO_FAKE_DIRECTOR", "").strip() in ("1", "true", "yes")
    # The narrator engine and the engine the notes are written for are two
    # different decisions: passing --target-engine breeze used to build a script
    # with *every* line on Breeze, so "direct my Kokoro book for Breeze" quietly
    # recast the book instead of directing it.
    script = _load_script(args.path, args.book_type, args.build_engine, args.casting_mode)

    target = args.target_engine
    if target == "auto":
        capable = [e for e, s in DEFAULT_SPECS.items() if s.get("instruction")]
        target = default_target_engine(script, capable)
        if not target:
            print(
                "no line of this book is routed to an instruction-capable engine "
                f"({', '.join(sorted(capable))}), so a pass would stamp no notes. "
                "Re-cast the book (--casting-mode dialogue_expressive) or name an "
                "engine with --target-engine; the pass will still carry pace and "
                "emotion, which every engine honours."
            )
            target = DEFAULT_SPECS and sorted(capable)[0] or ""
        print(f"target     resolved --target-engine auto -> {target}")
    spec = resolve_spec(target, None if fake else args.gateway)
    scenes = [s.strip() for s in args.scenes.split(",") if s.strip()] or None

    if fake:
        annotate: AnnotateFn = fake_annotate(script)
        model, prompt_version = FAKE_MODEL, FAKE_PROMPT_VERSION
    else:
        annotate = http_annotate(args.gateway, args.provider, args.model, args.api_key or None)
        model, prompt_version = args.model, _remote_prompt_version(args.gateway)

    print(f"script     {args.path}  ({len(script.utterances)} utterances)")
    print(f"director   {'fake (offline)' if fake else args.provider + ':' + args.model}")
    print(f"target     {target}  tags: {', '.join(spec.get('audio_tags') or []) or 'none'}")

    def progress(done: int, total: int, scene: PassScene) -> None:
        print(f"  [{done}/{total}] {scene.status:<8} {scene.scene_id}", flush=True)

    pass_ = run_pass(
        script,
        scenes=scenes,
        annotate=annotate,
        target_spec=spec,
        provider=args.provider,
        model=model,
        prompt_version=prompt_version,
        book=script.meta.title or "",
        on_progress=progress,
        cache={},
    )

    if args.apply:
        # Non-interactive shortcut: accept everything that survived validation.
        # The studio's review screen is where a human picks accepted / edited /
        # kept_text / rejected per scene; this is the headless equivalent.
        for scene in pass_.scenes:
            if scene.status == "kept":
                scene.decision = "accepted"
        changed = apply_pass(script, pass_)
        out_script = args.script_out or "script.json"
        with open(out_script, "w", encoding="utf-8") as fh:
            fh.write(script.model_dump_json(indent=2))
        print(f"\napplied    {len(changed)} lines stamped -> {out_script}")

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(pass_.model_dump_json(indent=2))
    _print_summary(pass_)
    print(f"\nwrote      {args.out}")
    if pass_.counts.rejected:
        print("\n-> REJECTED scenes kept the model's text; review the diff, then accept the note")
        print("   without the tags (kept_text) or retry the scene.")

    # Unlike scripts/try_director.py (a prompt-tuning tool, where a rejection is
    # the number you drive to zero), a rejection here is a normal review item and
    # not a failed run. Only a failed *call* is an error.
    return 1 if pass_.counts.error else 0


def _remote_prompt_version(gateway_url: str) -> str:
    """Ask the gateway which prompt it is running, for the cache key."""
    try:
        req = urllib.request.Request(f"{gateway_url.rstrip('/')}/api/director/defaults", method="GET")
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return str(json.loads(resp.read().decode("utf-8")).get("prompt_version") or "")
    except Exception:  # noqa: BLE001 - an unknown prompt version just disables cache reuse
        return ""


if __name__ == "__main__":
    raise SystemExit(main())
