"""Production profiles: the genre-driven "how should this book sound" settings.

A profile is a bundle of parameters that drive every downstream stage — narrator
voice, base speed, the pause budget, prose-vs-verse chunking, and which content
to strip (page numbers, footnote markers, citations, figure captions). One is
chosen per book (by ``classify``) and stored in the Book Script, so the output is
tailored to a novel vs. a textbook vs. a book of poems instead of one-size-fits-all.

All values are Kokoro-achievable: voice choice, pacing, pauses, structure, and
skip-rules. Per-line *emotional* delivery is deliberately absent (Kokoro has no
emotion knob) — that stays in the engine-agnostic ``intent`` field for a future
expressive engine.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

BOOK_TYPES = [
    "literary",
    "fantasy_scifi",
    "general_fiction",
    "nonfiction",
    "selfhelp",
    "poetry",
    "drama",
    "children",
    "technical",
    "unknown",
]


class ProductionProfile(BaseModel):
    book_type: str = "general_fiction"
    label: str = "General fiction"
    narrator_voice: str = "af_heart"
    base_speed: float = Field(default=1.0, gt=0.1, le=4.0)

    # Pause budget (ms).
    sentence_pause_ms: int = 200
    paragraph_pause_ms: int = 600
    scene_pause_ms: int = 1200
    chapter_pause_ms: int = 2000
    heading_pause_ms: int = 900

    # Structure.
    chunk_mode: str = "prose"  # "prose" | "verse"
    line_pause_ms: int = 380  # verse: pause after each line
    stanza_pause_ms: int = 850  # verse: pause between stanzas
    multi_voice: bool = False  # hint for the (future) casting stage
    read_headings: bool = True

    # Content skip-rules (the "don't read garbage aloud" levers).
    strip_page_numbers: bool = False
    strip_footnote_markers: bool = False
    strip_citations: bool = False
    strip_figure_captions: bool = False

    notes: str = ""


# The built-in palette. Voices are known-good Kokoro ids (af_heart = warm US
# female, am_michael = US male). Tune freely — this is the whole "art direction".
_PROFILES: dict[str, ProductionProfile] = {
    "general_fiction": ProductionProfile(
        book_type="general_fiction", label="General fiction", multi_voice=True,
    ),
    "literary": ProductionProfile(
        book_type="literary", label="Literary / classical", base_speed=0.96,
        sentence_pause_ms=220, paragraph_pause_ms=720, scene_pause_ms=1300,
        chapter_pause_ms=2200, multi_voice=True, strip_footnote_markers=True,
        notes="Measured pace; complex syntax; classics often carry footnotes.",
    ),
    "fantasy_scifi": ProductionProfile(
        book_type="fantasy_scifi", label="Fantasy / sci-fi", multi_voice=True,
        notes="Many characters + invented names → casting + pronunciation lexicon matter most.",
    ),
    "nonfiction": ProductionProfile(
        book_type="nonfiction", label="Nonfiction", narrator_voice="am_michael",
        base_speed=0.97, sentence_pause_ms=200, paragraph_pause_ms=650,
        scene_pause_ms=1100, chapter_pause_ms=2000, heading_pause_ms=1000,
        multi_voice=False, strip_page_numbers=True, strip_footnote_markers=True,
        strip_citations=True, strip_figure_captions=True,
        notes="Authoritative single narrator; strip apparatus (citations, notes, page numbers).",
    ),
    "selfhelp": ProductionProfile(
        book_type="selfhelp", label="Self-help / how-to", base_speed=1.0,
        sentence_pause_ms=220, paragraph_pause_ms=750, multi_voice=False,
        strip_page_numbers=True,
        notes="Warm, direct, second-person; lists/steps get breathing room.",
    ),
    "poetry": ProductionProfile(
        book_type="poetry", label="Poetry / verse", base_speed=0.9,
        chunk_mode="verse", line_pause_ms=380, stanza_pause_ms=900,
        chapter_pause_ms=2000, multi_voice=False,
        notes="Line breaks are semantic — one line per utterance, never merged.",
    ),
    "drama": ProductionProfile(
        book_type="drama", label="Play / drama", multi_voice=True,
        notes="Character cues are explicit speaker labels → easy attribution.",
    ),
    "children": ProductionProfile(
        book_type="children", label="Children's", base_speed=0.92,
        sentence_pause_ms=260, paragraph_pause_ms=750, scene_pause_ms=1300,
        multi_voice=True, notes="Slower, expressive, repetition-friendly.",
    ),
    "technical": ProductionProfile(
        book_type="technical", label="Technical / reference",
        narrator_voice="am_michael", base_speed=0.97, multi_voice=False,
        strip_page_numbers=True, strip_footnote_markers=True,
        strip_citations=True, strip_figure_captions=True,
        notes="Poor TTS fit (code/formulas/tables) — strip apparatus; consider skipping blocks.",
    ),
    "unknown": ProductionProfile(
        book_type="unknown", label="Unknown", multi_voice=False,
        notes="No confident classification — neutral narrative defaults.",
    ),
}


def get_profile(book_type: str) -> ProductionProfile:
    """Return a fresh copy of the profile for ``book_type`` (fallback: general)."""
    base = _PROFILES.get(book_type) or _PROFILES["general_fiction"]
    return base.model_copy(deep=True)
