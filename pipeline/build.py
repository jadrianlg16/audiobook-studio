"""Assemble a Book Script from a text file, driven by a production profile.

Flow: read text → classify book type → pick profile → ingest → clean (skip-rules)
→ chunk (prose sentences / verse lines). For multi-voice profiles (fiction), a
speaker-attribution pass splits narration vs. dialogue and assigns each quote a
character, then casting maps characters → distinct Kokoro voices.

    python -m pipeline.build book.txt --stats
    python -m pipeline.build book.txt --book-type poetry > book.json
    python -m pipeline.build novel.txt --single-voice   # force one narrator
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field

from .attribute import Attributer, is_dialogue_tag
from .cast import assign_engines, apply_cast_to_script, assign_voices
from .chunk import split_lines, split_sentences
from .classify import classify_book
from .clean import clean_paragraph
from .ingest import ingest_book
from .profiles import ProductionProfile, get_profile
from .schema import (
    Attribution,
    BookMeta,
    BookScript,
    CastEntry,
    Chapter,
    Render,
    Scene,
    Utterance,
    UtteranceKind,
)

DEFAULT_NARRATOR_VOICE = "af_heart"
_TURN_GAP_MS = 150  # extra pause when the speaker changes mid-paragraph
TAG_GAP_MS = 80
"""Silence around a dialogue tag. ``"You let it go out," she said.`` is one
breath group — a narrator reads straight through the comma — so the 200-350 ms
sentence pause that used to sit in the middle of it is a stutter on every tagged
line in the book. 80 ms is a voice change, not a beat."""

# Verse fingerprint for a poem sitting inside a prose book (an epigraph, a song,
# a letter in verse). Per-book classification cannot see it: the book is a novel.
_VERSE_MIN_LINES = 3
_VERSE_MAX_CHARS = 60
_VERSE_MIN_INDENTED = 2 / 3  # share of lines that must be indented


@dataclass
class _Unit:
    """One utterance-to-be, before its id and pauses are final."""

    speaker: str
    kind: UtteranceKind
    text: str
    basis: str = "none"
    confidence: float = 1.0
    lead_pause_ms: int = 0
    trail_pause_ms: int = 0
    scene_index: int = 0
    id: str = ""
    extra: dict = field(default_factory=dict)


def _looks_like_verse(paragraph: str) -> bool:
    """Is this block verse, whatever the book's profile says?

    Three or more consecutive short indented lines, most of them without terminal
    punctuation. A novel that contains a poem has no route to correct treatment
    otherwise — classification is per *book*, so the four lines get welded into
    one prose utterance and every line break is lost.
    """
    lines = [ln for ln in paragraph.split("\n") if ln.strip()]
    if len(lines) < _VERSE_MIN_LINES:
        return False
    if any(len(ln.strip()) >= _VERSE_MAX_CHARS for ln in lines):
        return False
    indented = sum(1 for ln in lines if ln[:1].isspace())
    if indented < _VERSE_MIN_INDENTED * len(lines):
        return False
    unpunctuated = sum(1 for ln in lines if ln.strip()[-1] not in ".!?:;")
    return unpunctuated * 2 > len(lines)


def _close_dialogue_tag_gaps(units: list[_Unit]) -> None:
    """Glue ``"…," she said.`` back onto its quote (in place).

    A tag is narration that directly follows a quote in the same paragraph. The
    quote's trailing pause drops to ``TAG_GAP_MS`` and the tag itself leads with
    none; when another quote from the *same* speaker follows the tag ("…," he
    answered. "There was nothing I could do."), the tag's own trail closes up too
    — that is one continuous line of speech with a name dropped into the middle.
    """
    for i, unit in enumerate(units):
        if i == 0 or unit.kind is not UtteranceKind.narration:
            continue
        previous = units[i - 1]
        if previous.kind is not UtteranceKind.dialogue or not is_dialogue_tag(unit.text):
            continue
        previous.trail_pause_ms = TAG_GAP_MS
        unit.lead_pause_ms = 0
        following = units[i + 1] if i + 1 < len(units) else None
        if (
            following is not None
            and following.kind is UtteranceKind.dialogue
            and following.speaker == previous.speaker
        ):
            unit.trail_pause_ms = TAG_GAP_MS


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n").replace("\r", "\n")


def build_book_script(
    path: str,
    *,
    narrator: str = "Narrator",
    voice_id: str | None = None,
    engine: str = "kokoro",
    title: str = "",
    author: str = "",
    book_type: str = "auto",
    profile: ProductionProfile | None = None,
    multi_voice: bool | None = None,
    use_llm: bool = False,
    llm_kwargs: dict | None = None,
    casting_mode: str = "narrator_only",
    expressive_engine: str = "breeze",
    engine_voices: dict[str, list[str]] | None = None,
) -> BookScript:
    # EPUBs (and anything else that is not plain text) are ingested first and the
    # classifier reads the extracted paragraphs; a .txt keeps its raw line breaks
    # because the verse fingerprint depends on them.
    raw = ingest_book(path)
    if path.lower().endswith((".txt", ".md", ".markdown", ".text")):
        raw_text = _read_text(path)
    else:
        raw_text = "\n\n".join(p for c in raw.chapters for p in c.paragraphs)
    title = title or raw.title
    author = author or raw.author

    if profile is None:
        if book_type == "auto":
            book_type = classify_book(raw_text, use_llm=use_llm, **(llm_kwargs or {}))
        profile = get_profile(book_type)
    if voice_id:  # explicit override wins over the profile's default voice
        profile.narrator_voice = voice_id
    narrator_voice = profile.narrator_voice

    verse = profile.chunk_mode == "verse"
    voices_on = (profile.multi_voice if multi_voice is None else multi_voice) and not verse

    script = BookScript(
        meta=BookMeta(
            title=title, author=author, source_path=path, book_type=profile.book_type
        ),
        profile=profile,
    )

    attributer = Attributer()
    order = 0
    chapter_number = 0  # counts narrative chapters only, so front matter never
    for ci, rc in enumerate(raw.chapters):  # shifts "Chapter 1" onto a copyright page
        if rc.kind == "chapter":
            chapter_number += 1
        chapter = Chapter(
            index=ci,
            title=rc.title or (f"Chapter {chapter_number}" if rc.kind == "chapter" else ""),
            kind=rc.kind,
            include_in_audio=rc.include_in_audio,
        )
        # Speaker state is per chapter. Carrying it across the boundary voiced
        # the first quote of every chapter as whoever spoke last in the previous
        # one; gender learned about a character is kept (see Attributer.reset).
        attributer.reset()
        scene_starts = set(rc.scene_starts)
        scene_index = 0
        chapter.scenes.append(Scene(index=0))
        units: list[_Unit] = []

        # The chapter announcement, when the profile asks for one. `read_headings`
        # was declared in every profile and consumed by nothing, so no book this
        # tool produced ever said its own chapter titles out loud.
        #
        # Only a heading the *source* actually carried is spoken: `Chapter.title`
        # falls back to "Chapter N" for display, and announcing a title the book
        # never had would put words in it (and would narrate "Chapter 1" over an
        # untitled poem).
        if profile.read_headings and rc.title and chapter.include_in_audio:
            units.append(
                _Unit(
                    speaker=narrator,
                    kind=UtteranceKind.heading,
                    text=chapter.title,
                    trail_pause_ms=profile.heading_pause_ms,
                    scene_index=0,
                    id=f"c{ci}_head",
                )
            )

        # Footnote *bodies*: a paragraph that starts with the number of a marker
        # this chapter carries ("1. The ledger has since been lost.") is apparatus,
        # not narration — the marker is already stripped, so read aloud it is a
        # numbered non-sequitur in the middle of a scene.
        footnote_numbers = set(re.findall(r"\[(\d{1,3})\]", " ".join(rc.paragraphs)))
        for pi, paragraph in enumerate(rc.paragraphs):
            enum = re.match(r"^\s*(\d{1,3})\.\s", paragraph)
            if enum and enum.group(1) in footnote_numbers:
                continue
            if pi in scene_starts and pi != 0:
                scene_index += 1
                chapter.scenes.append(Scene(index=scene_index))

            cleaned = clean_paragraph(paragraph, profile)
            if not cleaned:
                continue
            next_starts_scene = (pi + 1) in scene_starts
            as_verse = verse or _looks_like_verse(paragraph)

            para: list[_Unit] = []
            if as_verse:
                para = [
                    _Unit(narrator, UtteranceKind.verse, ln) for ln in split_lines(cleaned)
                ]
            elif voices_on:
                for turn in attributer.attribute(cleaned):
                    kind = (
                        UtteranceKind.dialogue
                        if turn.kind == "dialogue"
                        else UtteranceKind.narration
                    )
                    para += [
                        _Unit(turn.speaker, kind, s, turn.basis, turn.confidence)
                        for s in split_sentences(turn.text)
                    ]
            else:
                para = [
                    _Unit(narrator, UtteranceKind.narration, s)
                    for s in split_sentences(cleaned)
                ]

            for si, unit in enumerate(para):
                last = si == len(para) - 1
                if last:
                    if as_verse:
                        trail = profile.scene_pause_ms if next_starts_scene else profile.stanza_pause_ms
                    else:
                        trail = profile.scene_pause_ms if next_starts_scene else profile.paragraph_pause_ms
                elif as_verse:
                    trail = profile.line_pause_ms
                else:
                    base = profile.sentence_pause_ms
                    trail = base + _TURN_GAP_MS if para[si + 1].speaker != unit.speaker else base
                unit.trail_pause_ms = trail
                unit.scene_index = scene_index
                unit.id = f"c{ci}_p{pi}_s{si}"

            _close_dialogue_tag_gaps(para)
            units += para

        for unit in units:
            script.utterances.append(
                Utterance(
                    id=unit.id,
                    order=order,
                    chapter_index=ci,
                    scene_index=unit.scene_index,
                    speaker=unit.speaker,
                    kind=unit.kind,
                    text=unit.text,
                    attribution=Attribution(basis=unit.basis, confidence=unit.confidence),
                    render=Render(
                        engine=engine, voice_id=narrator_voice,
                        speed=profile.base_speed,
                        lead_pause_ms=unit.lead_pause_ms,
                        trail_pause_ms=unit.trail_pause_ms,
                    ),
                )
            )
            order += 1

        if script.utterances and script.utterances[-1].chapter_index == ci:
            script.utterances[-1].render.trail_pause_ms = profile.chapter_pause_ms
        script.chapters.append(chapter)

    _apply_cast(script, attributer, narrator, narrator_voice, engine, voices_on)

    # Hybrid casting: move some/all characters onto the expressive engine.
    # A no-op at the default ("narrator_only") so existing callers/behaviour
    # are unaffected — this only runs when a caller opts in.
    if casting_mode != "narrator_only":
        hybrid_cast = assign_engines(
            script.cast,
            mode=casting_mode,
            narrator_engine=engine,
            expressive_engine=expressive_engine,
            engine_voices=engine_voices,
            narrator=narrator,
        )
        apply_cast_to_script(script, hybrid_cast, narrator=narrator)

    return script


def _apply_cast(
    script: BookScript, attributer: Attributer, narrator: str,
    narrator_voice: str, engine: str, voices_on: bool,
) -> None:
    """Build the cast sheet and stamp each utterance with its character's voice."""
    if not voices_on:
        script.cast.append(CastEntry(character=narrator, voice_id=narrator_voice, engine=engine))
        return

    speakers: list[str] = []
    for u in script.utterances:
        if u.speaker != narrator and u.speaker not in speakers:
            speakers.append(u.speaker)

    genders = attributer.ctx.characters
    cast_map = assign_voices(speakers, genders, narrator_voice)
    # An unattributed quote is read by the narrator, the audiobook convention;
    # a designed "Unknown" voice would split one character across two voices.
    if "Unknown" in cast_map:
        cast_map["Unknown"] = narrator_voice

    script.cast.append(CastEntry(character=narrator, voice_id=narrator_voice, engine=engine))
    for name in speakers:
        script.cast.append(
            CastEntry(character=name, voice_id=cast_map[name], engine=engine, notes=genders.get(name, "n"))
        )
    for u in script.utterances:
        u.render.voice_id = cast_map.get(u.speaker, narrator_voice)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Build a Book Script JSON from a text file.")
    ap.add_argument("path", help="path to a .txt / markdown book file")
    ap.add_argument("--voice", default=None, help="override the profile's narrator voice")
    ap.add_argument("--book-type", default="auto", help="auto | literary | nonfiction | poetry | ...")
    ap.add_argument("--title", default="")
    ap.add_argument("--author", default="")
    ap.add_argument("--single-voice", action="store_true", help="force one narrator (no attribution)")
    ap.add_argument("--multi-voice", action="store_true", help="force multi-voice even for non-fiction")
    ap.add_argument("--stats", action="store_true", help="print a summary instead of full JSON")
    args = ap.parse_args(argv)

    mv = True if args.multi_voice else False if args.single_voice else None
    script = build_book_script(
        args.path, voice_id=args.voice, book_type=args.book_type,
        title=args.title, author=args.author, multi_voice=mv,
    )
    if args.stats:
        stats = script.stats()
        stats["cast"] = [f"{c.character} -> {c.voice_id}" for c in script.cast]
        print(json.dumps(stats, indent=2))
    else:
        print(script.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
