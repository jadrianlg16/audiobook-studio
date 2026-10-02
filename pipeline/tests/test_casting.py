"""Hybrid-casting tests. Deterministic, offline."""

from __future__ import annotations

import hashlib
import json

from pipeline.build import build_book_script
from pipeline.cast import (
    CASTING_MODES,
    FEMALE_VOICES,
    MALE_VOICES,
    apply_cast_to_script,
    assign_engines,
    licence_note_for,
    non_commercial_engines,
)
from pipeline.render import Renderer, fake_synth, http_synth
from pipeline.schema import BookScript, CastEntry, Chapter, Render, Utterance, UtteranceKind

DIALOGUE = """Chapter One

Elara pushed open the heavy door. "Is anyone there?" she called.

"Only me," said Marcus. He stepped out of the shadows. "You're late."

"The roads were flooded," Elara replied."""


def _hybrid_cast(mode: str) -> list[CastEntry]:
    """A plain kokoro cast (narrator + two genders) as it looks right after
    ``_apply_cast``, before any hybrid engine assignment."""
    cast = [
        CastEntry(character="Narrator", voice_id="af_heart", engine="kokoro", notes=""),
        CastEntry(character="Elara", voice_id="af_bella", engine="kokoro", notes="f"),
        CastEntry(character="Marcus", voice_id="am_michael", engine="kokoro", notes="m"),
    ]
    return assign_engines(cast, mode=mode)


# ---- assign_engines -----------------------------------------------------


def test_narrator_only_puts_everyone_on_kokoro_with_distinct_named_voices():
    cast = _hybrid_cast("narrator_only")
    by_name = {c.character: c for c in cast}
    assert all(c.engine == "kokoro" for c in cast)
    assert all(c.voice_design is None for c in cast)
    assert by_name["Elara"].voice_id in FEMALE_VOICES
    assert by_name["Marcus"].voice_id in MALE_VOICES
    voice_ids = {c.voice_id for c in cast}
    assert len(voice_ids) == 3  # narrator + 2 characters, all distinct


def test_dialogue_expressive_moves_only_non_narrator_characters():
    cast = _hybrid_cast("dialogue_expressive")
    by_name = {c.character: c for c in cast}

    assert by_name["Narrator"].engine == "kokoro"
    assert by_name["Narrator"].voice_id is not None
    assert by_name["Narrator"].voice_design is None

    for name in ("Elara", "Marcus"):
        assert by_name[name].engine == "breeze"
        assert by_name[name].voice_id is None
        assert by_name[name].voice_design  # non-empty description


def test_all_expressive_moves_everyone_including_the_narrator():
    cast = _hybrid_cast("all_expressive")
    assert all(c.engine == "breeze" for c in cast)
    assert all(c.voice_id is None for c in cast)
    assert all(c.voice_design for c in cast)


def test_voice_design_is_distinct_per_gender_by_first_appearance_order():
    cast = [
        CastEntry(character="Narrator", voice_id="af_heart", engine="kokoro"),
        CastEntry(character="Nadia", voice_id="af_bella", engine="kokoro", notes="f"),
        CastEntry(character="Elara", voice_id="af_nicole", engine="kokoro", notes="f"),
        CastEntry(character="Marcus", voice_id="am_michael", engine="kokoro", notes="m"),
    ]
    hybrid = assign_engines(cast, mode="all_expressive")
    by_name = {c.character: c for c in hybrid}

    # Two females get two different designs, in first-appearance order.
    assert by_name["Nadia"].voice_design != by_name["Elara"].voice_design
    # The narrator gets a narrator-flavoured description, distinct from cast members.
    assert by_name["Narrator"].voice_design not in (
        by_name["Nadia"].voice_design,
        by_name["Elara"].voice_design,
        by_name["Marcus"].voice_design,
    )


def test_assign_engines_is_deterministic():
    cast = [
        CastEntry(character="Narrator", voice_id="af_heart", engine="kokoro"),
        CastEntry(character="Elara", voice_id="af_bella", engine="kokoro", notes="f"),
        CastEntry(character="Marcus", voice_id="am_michael", engine="kokoro", notes="m"),
    ]
    a = assign_engines(cast, mode="dialogue_expressive")
    b = assign_engines(cast, mode="dialogue_expressive")
    assert a == b


def test_engine_absent_from_engine_voices_dict_means_no_named_voices():
    cast = [
        CastEntry(character="Narrator", voice_id="af_heart", engine="kokoro"),
        CastEntry(character="Elara", voice_id="af_bella", engine="kokoro", notes="f"),
    ]
    # Only kokoro is listed; the target ("breeze") is absent -> no named voices.
    hybrid = assign_engines(
        cast, mode="all_expressive", expressive_engine="breeze", engine_voices={"kokoro": FEMALE_VOICES}
    )
    assert all(c.voice_id is None and c.voice_design for c in hybrid)


def test_engine_voices_can_grant_a_target_engine_named_voices():
    cast = [
        CastEntry(character="Narrator", voice_id="af_heart", engine="kokoro"),
        CastEntry(character="Elara", voice_id="af_bella", engine="kokoro", notes="f"),
    ]
    hybrid = assign_engines(
        cast, mode="all_expressive", expressive_engine="breeze",
        engine_voices={"breeze": ["breeze-1", "breeze-2"]},
    )
    voice_ids = {c.voice_id for c in hybrid}
    assert None not in voice_ids
    assert voice_ids <= {"breeze-1", "breeze-2"}
    assert all(c.voice_design is None for c in hybrid)


def test_unknown_casting_mode_raises():
    cast = [CastEntry(character="Narrator", voice_id="af_heart", engine="kokoro")]
    try:
        assign_engines(cast, mode="bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass


# ---- apply_cast_to_script -------------------------------------------------


def _tiny_script() -> BookScript:
    script = BookScript()
    script.chapters.append(Chapter(index=0, title="One"))
    script.cast = [
        CastEntry(character="Narrator", voice_id="af_heart", engine="kokoro", notes=""),
        CastEntry(character="Elara", voice_id="af_bella", engine="kokoro", notes="f"),
    ]
    script.utterances = [
        Utterance(
            id="u0", order=0, chapter_index=0, speaker="Narrator", kind=UtteranceKind.narration,
            text="The door creaked.", render=Render(engine="kokoro", voice_id="af_heart"),
        ),
        Utterance(
            id="u1", order=1, chapter_index=0, speaker="Elara", kind=UtteranceKind.dialogue,
            text="Who's there?", render=Render(engine="kokoro", voice_id="af_bella"),
        ),
    ]
    return script


def test_apply_cast_to_script_stamps_narration_vs_dialogue_and_reports_changed_ids():
    script = _tiny_script()
    hybrid = assign_engines(script.cast, mode="dialogue_expressive")
    changed = apply_cast_to_script(script, hybrid)

    by_id = {u.id: u for u in script.utterances}
    assert by_id["u0"].render.engine == "kokoro"          # narration -> narrator's entry
    assert by_id["u0"].render.voice_design is None
    assert by_id["u1"].render.engine == "breeze"           # dialogue -> Elara's own entry
    assert by_id["u1"].render.voice_design
    assert by_id["u1"].render.voice_id is None

    # u0 (Narrator) was already kokoro/af_heart before the pass, so it isn't
    # reported changed; u1 (Elara) actually moved engine/voice -> reported.
    assert changed == ["u1"]
    assert [c.character for c in script.cast] == ["Narrator", "Elara"]


def test_apply_cast_to_script_is_idempotent_on_the_second_pass():
    script = _tiny_script()
    hybrid = assign_engines(script.cast, mode="dialogue_expressive")
    apply_cast_to_script(script, hybrid)
    changed_again = apply_cast_to_script(script, hybrid)  # same cast, nothing left to change
    assert changed_again == []


# ---- licence surfacing -----------------------------------------------------


def test_non_commercial_engines_contains_breeze():
    assert "breeze" in non_commercial_engines()


def test_licence_note_none_for_kokoro_only_cast():
    cast = [CastEntry(character="Narrator", voice_id="af_heart", engine="kokoro")]
    assert licence_note_for(cast) is None


def test_licence_note_set_when_breeze_is_in_the_cast():
    cast = [
        CastEntry(character="Narrator", voice_id="af_heart", engine="kokoro"),
        CastEntry(character="Elara", engine="breeze", voice_design="a warm voice"),
    ]
    note = licence_note_for(cast)
    assert note is not None
    assert "breeze" in note.lower()


# ---- cache key: voice_design is part of it, only when set ------------------


def test_cache_key_changes_when_voice_design_is_set(tmp_path):
    renderer = Renderer(fake_synth(), cache_dir=str(tmp_path / "cache"))
    base = Render(engine="breeze", voice_id=None)
    designed = Render(engine="breeze", voice_id=None, voice_design="a warm, low adult female voice")
    other_design = Render(engine="breeze", voice_id=None, voice_design="a deep, calm adult male voice")

    k_base = renderer.cache_key("Hello.", base, "0.1.0")
    k_designed = renderer.cache_key("Hello.", designed, "0.1.0")
    k_other = renderer.cache_key("Hello.", other_design, "0.1.0")

    assert k_designed != k_base
    assert k_designed != k_other
    assert renderer.cache_key("Hello.", designed.model_copy(), "0.1.0") == k_designed


def test_cache_key_with_no_voice_design_matches_the_pre_x3_formula(tmp_path):
    """A book rendered before voice_design existed must keep its cache — the
    key with voice_design unset has to hash exactly as it did before."""
    legacy = hashlib.sha256(
        "0.1.0\x00kokoro\x00af_heart\x001.0\x00Hello.".encode("utf-8")
    ).hexdigest()[:32]
    renderer = Renderer(fake_synth(), cache_dir=str(tmp_path / "cache"))
    key = renderer.cache_key("Hello.", Render(engine="kokoro", voice_id="af_heart"), "0.1.0")
    assert key == legacy


# ---- http_synth payload composition ---------------------------------------


class _Resp:
    def __init__(self, body: bytes = b"RIFFfake"):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_http_synth_sends_voice_design_alone(monkeypatch):
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    synth = http_synth("http://gw", engine="kokoro")
    synth("Hello.", Render(engine="breeze", voice_id=None, voice_design="a warm, low female voice"))
    assert seen["body"]["instruction"] == "a warm, low female voice"
    assert seen["body"]["voice"] is None


def test_http_synth_composes_voice_design_and_instruction(monkeypatch):
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    synth = http_synth("http://gw", engine="kokoro")
    synth(
        "Hello.",
        Render(engine="breeze", voice_id=None, voice_design="a warm, low female voice", instruction="Slow and sad."),
    )
    assert seen["body"]["instruction"] == "a warm, low female voice. Slow and sad."


def test_http_synth_omits_instruction_key_when_neither_is_set(monkeypatch):
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    synth = http_synth("http://gw", engine="kokoro")
    synth("Hello.", Render(engine="kokoro", voice_id="af_heart"))
    assert "instruction" not in seen["body"]
    assert seen["body"]["voice"] == "af_heart"


# ---- end-to-end: build_book_script with casting_mode -----------------------


def test_build_book_script_dialogue_expressive_end_to_end(tmp_path):
    p = tmp_path / "d.txt"
    p.write_text(DIALOGUE, encoding="utf-8")
    script = build_book_script(str(p), book_type="general_fiction", casting_mode="dialogue_expressive")

    by_name = {c.character: c for c in script.cast}
    assert by_name["Narrator"].engine == "kokoro"
    assert by_name["Narrator"].voice_id is not None
    assert by_name["Elara"].engine == "breeze"
    assert by_name["Elara"].voice_id is None
    assert by_name["Elara"].voice_design
    assert by_name["Marcus"].engine == "breeze"
    assert by_name["Marcus"].voice_id is None
    assert by_name["Marcus"].voice_design
    assert by_name["Elara"].voice_design != by_name["Marcus"].voice_design

    # Utterances themselves carry the same routing as the cast sheet — this is
    # the "cross-chapter voice memory": every Elara line, in any chapter, reads
    # her cast entry, so it's automatically consistent everywhere she appears.
    elara_lines = [u for u in script.utterances if u.speaker == "Elara"]
    assert elara_lines and all(u.render.engine == "breeze" for u in elara_lines)
    assert all(u.render.voice_design == by_name["Elara"].voice_design for u in elara_lines)
    narrator_lines = [u for u in script.utterances if u.speaker == "Narrator"]
    assert narrator_lines and all(u.render.engine == "kokoro" for u in narrator_lines)


def test_build_book_script_default_casting_mode_is_unchanged(tmp_path):
    """Default behaviour (no casting_mode passed) must be identical to before
    hybrid casting existed: everyone on kokoro, named voices only."""
    p = tmp_path / "d.txt"
    p.write_text(DIALOGUE, encoding="utf-8")
    script = build_book_script(str(p), book_type="general_fiction")
    assert all(c.engine == "kokoro" for c in script.cast)
    assert all(c.voice_design is None for c in script.cast)


def test_casting_modes_tuple_has_the_three_documented_values():
    assert CASTING_MODES == ("narrator_only", "dialogue_expressive", "all_expressive")
