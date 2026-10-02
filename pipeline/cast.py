"""Casting: map each speaker to a distinct Kokoro voice, gender-aware.

Voices are the ids Kokoro-82M actually advertises (engines/kokoro/tts.py). The
narrator keeps the profile's voice; characters get distinct voices from the
gender-appropriate pool, avoiding the narrator's voice and each other's until the
pool is exhausted (then it cycles deterministically). Assignment is stable across
runs — same book → same cast → cache hits on re-render.

Hybrid casting (below ``assign_voices``) layers *engine* selection on top of
this: ``assign_engines`` moves some or all characters onto an expressive engine
(Breeze) per ``Settings.casting_mode``. An engine with no named voices designs a
voice from a prose description instead (``CastEntry.voice_design``), so casting
such an engine means picking a description, not a voice id.
"""

from __future__ import annotations

from .schema import BookScript, CastEntry, UtteranceKind

# Kokoro voice ids by perceived gender (from the engine's advertised set).
FEMALE_VOICES = ["af_heart", "af_bella", "af_nicole", "bf_emma"]
MALE_VOICES = ["am_michael", "am_fenrir", "bm_george"]
# Fallback order for unknown-gender speakers: alternate f/m for variety.
NEUTRAL_ORDER = ["af_bella", "am_michael", "af_nicole", "am_fenrir", "bf_emma", "bm_george", "af_heart"]


def assign_voices(
    speakers: list[str], genders: dict[str, str], narrator_voice: str = "af_heart"
) -> dict[str, str]:
    """Return ``{speaker: voice_id}`` including "Narrator". ``speakers`` is the
    distinct non-narrator speakers in first-appearance order."""
    cast: dict[str, str] = {"Narrator": narrator_voice}
    used: set[str] = {narrator_voice}
    counters = {"f": 0, "m": 0, "n": 0}

    def pick(gender: str) -> str:
        pool = FEMALE_VOICES if gender == "f" else MALE_VOICES if gender == "m" else NEUTRAL_ORDER
        for v in pool:
            if v not in used:
                used.add(v)
                return v
        v = pool[counters[gender if gender in counters else "n"] % len(pool)]
        counters[gender if gender in counters else "n"] += 1
        return v

    for name in speakers:
        if name in cast:
            continue
        cast[name] = pick(genders.get(name, "n"))
    return cast


# ---- hybrid casting (engine selection + voice design) -----------------------

CASTING_MODES = ("narrator_only", "dialogue_expressive", "all_expressive")

# The only engine with named voices today. Kept as a plain default so a caller
# who doesn't pass ``engine_voices`` gets the real Kokoro ids (same list this
# module already uses above) rather than an engine silently having none.
DEFAULT_ENGINE_VOICES: dict[str, list[str]] = {"kokoro": FEMALE_VOICES + MALE_VOICES}

# Small deterministic palette for engines with no named voices (Breeze designs a
# voice from prose instead of picking one). Cycled by first-appearance order
# within each gender so two characters sharing a gender still sound distinct.
FEMALE_VOICE_DESIGNS = [
    "a warm, low adult female voice, unhurried",
    "a bright, quick young adult female voice",
    "a crisp, precise older female voice",
]
MALE_VOICE_DESIGNS = [
    "a deep, calm adult male voice, unhurried",
    "a warm, gravelly older male voice",
    "a bright, energetic young adult male voice",
]
NEUTRAL_VOICE_DESIGNS = [
    "a clear, even adult voice, gender-neutral",
    "a soft, measured adult voice, gender-neutral",
]
NARRATOR_VOICE_DESIGN = "a warm, steady narrator's voice, clear diction, unhurried pacing"

# Utterance kinds that are always read by the narrator regardless of the
# paragraph's speaker (mirrors build.py's own narration/verse handling).
_NARRATOR_KINDS = frozenset({UtteranceKind.narration, UtteranceKind.heading, UtteranceKind.verse})


def _target_engine(mode: str, is_narrator: bool, narrator_engine: str, expressive_engine: str) -> str:
    if mode == "narrator_only":
        return narrator_engine
    if mode == "dialogue_expressive":
        return narrator_engine if is_narrator else expressive_engine
    if mode == "all_expressive":
        return expressive_engine
    raise ValueError(f"unknown casting mode {mode!r}; must be one of {CASTING_MODES}")


def _pick_voice_design(gender: str, is_narrator: bool, counters: dict[str, int]) -> str:
    if is_narrator:
        return NARRATOR_VOICE_DESIGN
    pool = FEMALE_VOICE_DESIGNS if gender == "f" else MALE_VOICE_DESIGNS if gender == "m" else NEUTRAL_VOICE_DESIGNS
    key = gender if gender in ("f", "m") else "n"
    idx = counters.get(key, 0)
    counters[key] = idx + 1
    return pool[idx % len(pool)]


def _pick_named_voice(
    target_engine: str,
    gender: str,
    is_narrator: bool,
    available: list[str],
    used: set[str],
    counters: dict[str, int],
) -> str:
    if target_engine == "kokoro" and not is_narrator:
        base_pool = FEMALE_VOICES if gender == "f" else MALE_VOICES if gender == "m" else NEUTRAL_ORDER
        pool = [v for v in base_pool if v in available] or list(available)
    else:
        pool = list(available)
    for v in pool:
        if v not in used:
            used.add(v)
            return v
    # Pool exhausted (more characters than voices): cycle deterministically
    # rather than collide silently, same spirit as assign_voices above.
    key = f"{target_engine}:{gender if gender in ('f', 'm') else 'n'}"
    idx = counters.get(key, 0)
    counters[key] = idx + 1
    return pool[idx % len(pool)]


def assign_engines(
    cast: list[CastEntry],
    *,
    mode: str,
    narrator_engine: str = "kokoro",
    expressive_engine: str = "breeze",
    engine_voices: dict[str, list[str]] | None = None,
    narrator: str = "Narrator",
) -> list[CastEntry]:
    """Return a **new** cast with each entry's engine chosen per ``mode``:

    - ``narrator_only``: everyone on ``narrator_engine`` (today's behaviour).
    - ``dialogue_expressive``: every character except the narrator moves to
      ``expressive_engine``.
    - ``all_expressive``: everyone (narrator included) moves to
      ``expressive_engine``.

    When a character lands on an engine with no named voices (absent from
    ``engine_voices``, default: only "kokoro" has any), ``voice_id`` is cleared
    and ``voice_design`` is filled from the character's gender note (a small
    deterministic palette, picked by first-appearance order so two characters
    of the same gender still sound different). Moving onto an engine that does
    have named voices picks one with the same gender-aware logic as
    ``assign_voices``. Pure function of ``cast`` + these keyword args, so the
    same book produces the same cast every time — that stability is what keeps
    the render cache warm across re-renders.
    """
    if mode not in CASTING_MODES:
        raise ValueError(f"unknown casting mode {mode!r}; must be one of {CASTING_MODES}")
    voices_by_engine = DEFAULT_ENGINE_VOICES if engine_voices is None else engine_voices

    used_named: set[str] = set()
    named_counters: dict[str, int] = {}
    design_counters: dict[str, int] = {}

    new_cast: list[CastEntry] = []
    for entry in cast:
        # "Unknown" rides with the narrator: same engine, same voice (build.py
        # already gives it the narrator voice), so an unattributed quote never
        # becomes a third voice in a two-person scene.
        is_narrator = entry.character in (narrator, "Unknown")
        target_engine = _target_engine(mode, is_narrator, narrator_engine, expressive_engine)
        available = voices_by_engine.get(target_engine)
        gender = entry.notes if entry.notes in ("f", "m") else "n"

        if available:
            voice_id = _pick_named_voice(target_engine, gender, is_narrator, available, used_named, named_counters)
            voice_design = None
        else:
            voice_id = None
            voice_design = _pick_voice_design(gender, is_narrator, design_counters)

        new_cast.append(
            entry.model_copy(update={"engine": target_engine, "voice_id": voice_id, "voice_design": voice_design})
        )
    return new_cast


def apply_cast_to_script(script: BookScript, cast: list[CastEntry], *, narrator: str = "Narrator") -> list[str]:
    """Re-stamp ``render.engine``/``render.voice_id``/``render.voice_design`` on
    every utterance from its speaker's cast entry, replace ``script.cast``, and
    return the ids of utterances whose render actually changed.

    Narration/heading/verse lines always take the narrator's entry regardless
    of the stamped ``speaker`` field; everything else (dialogue, internal
    thought, letters) takes its own speaker's entry. This is the book's
    "cross-chapter voice memory": the cast sheet is book-level, so re-running
    this after any cast edit keeps a character's voice + engine + voice
    description consistent everywhere they appear.
    """
    by_character = {c.character: c for c in cast}
    narrator_entry = by_character.get(narrator)
    changed: list[str] = []

    for u in script.utterances:
        entry = narrator_entry if u.kind in _NARRATOR_KINDS else by_character.get(u.speaker, narrator_entry)
        if entry is None:
            continue
        before = (u.render.engine, u.render.voice_id, u.render.voice_design)
        after = (entry.engine, entry.voice_id, entry.voice_design)
        if before != after:
            u.render.engine = entry.engine
            u.render.voice_id = entry.voice_id
            u.render.voice_design = entry.voice_design
            changed.append(u.id)

    script.cast = list(cast)
    return changed


def non_commercial_engines() -> set[str]:
    """Engines whose weights are research/non-commercial, including self-hosted
    output — see engines/breeze/README.md. Small and manual on purpose: this is
    a licence fact about the weights, not something to infer from engine
    metadata that could drift silently."""
    return {"breeze"}


def licence_note_for(cast: list[CastEntry]) -> str | None:
    """None unless the cast routes at least one character through a
    non-commercial engine, in which case a human-readable note naming it —
    the studio surfaces this on the shelf row and the desk (Semantic #7)."""
    flagged = sorted({c.engine for c in cast if c.engine in non_commercial_engines()})
    if not flagged:
        return None
    names = ", ".join(flagged)
    return (
        f"This book routes voices through non-commercial engine(s): {names}. "
        "Their weights (including self-hosted output) are licensed for research "
        "and personal use only — not for a commercially published audiobook."
    )
