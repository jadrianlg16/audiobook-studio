"""Director tests for the pieces Breeze TTS 2 added: prose instructions and
inline audio tags.

The important one is ``_validate_tagged_text``. Asking an LLM to annotate a
passage invites it to *improve* the passage, and a paraphrase that slips through
puts words in the audiobook that the author never wrote — silently, because the
audio still sounds fine. So the rule is: strip the tags back out, and what's left
must be the original, character for character.

Run from the project root:  pytest gateway/tests
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from gateway.app.main import create_app
from gateway.app.models import DirectorEngineSpec, DirectorRequest
from gateway.app.services import director as d

_TAGS = ["(sigh)", "(laugh)", "(clears throat)"]
_SOURCE = "She put the letter down. It was over, and she had known for weeks."


# ---- tag-annotated text: the anti-paraphrase guard --------------------------

def test_accepts_tags_inserted_into_the_original_wording() -> None:
    got = d._validate_tagged_text(
        "(sigh) She put the letter down. It was over, and she had known for weeks.",
        _SOURCE,
        _TAGS,
    )
    assert got is not None
    assert got.startswith("(sigh)")


def test_rejects_a_paraphrase() -> None:
    """The failure mode that matters: plausible, fluent, and not the book."""
    assert (
        d._validate_tagged_text(
            "(sigh) She set the letter down. It was finished, and she had known for weeks.",
            _SOURCE,
            _TAGS,
        )
        is None
    )


def test_rejects_a_truncated_passage() -> None:
    assert d._validate_tagged_text("(sigh) She put the letter down.", _SOURCE, _TAGS) is None


def test_rejects_a_tag_the_engine_does_not_know() -> None:
    """An unlisted tag would be read aloud as literal words."""
    assert (
        d._validate_tagged_text(
            "(whistles) She put the letter down. It was over, and she had known for weeks.",
            _SOURCE,
            _TAGS,
        )
        is None
    )


def test_returns_none_when_no_tag_was_actually_added() -> None:
    assert d._validate_tagged_text(_SOURCE, _SOURCE, _TAGS) is None


def test_returns_none_for_engines_with_no_tag_vocabulary() -> None:
    assert d._validate_tagged_text(f"(sigh) {_SOURCE}", _SOURCE, []) is None


def test_whitespace_differences_are_tolerated() -> None:
    """Removing a tag leaves a double space; models also reflow newlines. Neither
    changes a single spoken word, so neither should reject an otherwise good take."""
    got = d._validate_tagged_text(
        "She put the letter down.  (sigh)\nIt was over, and she had known for weeks.",
        _SOURCE,
        _TAGS,
    )
    assert got is not None


# ---- near-miss tag spelling -------------------------------------------------
# Observed with gemma3:12b: told the tag is `(pause)`, it wrote `[pause]`. Right
# word, right place, wrong bracket. Repairing that is the difference between a
# 12B model being usable as a director and being a coin flip.

def test_square_bracket_tag_is_normalized_to_the_engines_form() -> None:
    got = d._validate_tagged_text(
        "She put the letter down. [sigh] It was over, and she had known for weeks.",
        _SOURCE,
        _TAGS,
    )
    assert got is not None
    assert "(sigh)" in got and "[sigh]" not in got


def test_tag_normalization_is_case_and_spacing_insensitive() -> None:
    got = d._validate_tagged_text(f"<  Sigh > {_SOURCE}", _SOURCE, _TAGS)
    assert got is not None and got.startswith("(sigh)")


def test_tag_placed_before_punctuation_is_accepted() -> None:
    """Observed with gemma3:12b: 'again. Outside' annotated as 'again (pause).
    Outside'. The words are untouched — stripping the tag just leaves a space in
    front of the period. Rejecting that would throw away a correct annotation."""
    got = d._validate_tagged_text(
        "She put the letter down (pause). It was over, and she had known for weeks.",
        "She put the letter down. It was over, and she had known for weeks.",
        _TAGS + ["(pause)"],
    )
    assert got is not None and "(pause)" in got


def test_normalizing_a_bracket_does_not_rescue_a_paraphrase() -> None:
    """The repair must only ever touch tags — the word check still decides."""
    assert (
        d._validate_tagged_text(
            "[sigh] She set the letter down. It was finished, and she had known for weeks.",
            _SOURCE,
            _TAGS,
        )
        is None
    )


def test_a_passage_that_really_contains_bracketed_words_fails_closed() -> None:
    """A play script with a literal '[sigh]' in the text must not be silently
    rewritten. Rejecting the annotation is the safe outcome."""
    source = "HAMLET [sigh] speaks at last."
    assert d._validate_tagged_text("HAMLET [sigh] speaks at last.", source, _TAGS) is None


# ---- instructions -----------------------------------------------------------

def test_instruction_is_dropped_for_engines_that_cannot_take_one() -> None:
    assert d._clean_instruction("Speak slowly.", allowed=False) is None


def test_instruction_is_kept_and_normalized() -> None:
    assert d._clean_instruction("  Speak\n slowly.  ", allowed=True) == "Speak slowly."


def test_instruction_nulls_are_treated_as_absent() -> None:
    for junk in ("null", "None", "n/a", "  "):
        assert d._clean_instruction(junk, allowed=True) is None


def test_long_instruction_is_truncated() -> None:
    got = d._clean_instruction("word " * 500, allowed=True)
    assert got is not None and len(got) <= d._MAX_INSTRUCTION_CHARS


# ---- per-engine validation end to end ---------------------------------------

def test_validate_per_engine_routes_direction_to_the_right_engines() -> None:
    engines = [
        DirectorEngineSpec(id="breeze", instruction=True, audio_tags=_TAGS),
        DirectorEngineSpec(id="kokoro", voices=["af_heart"]),
    ]
    raw = [
        {
            "engine_id": "breeze",
            "instruction": "Slow, restrained, holding the last word.",
            "text": f"(sigh) {_SOURCE}",
            "scalar": 0.7,
        },
        {
            # kokoro can do neither — both must be stripped, not passed through.
            "engine_id": "kokoro",
            "instruction": "Slow and restrained.",
            "text": f"(sigh) {_SOURCE}",
            "voice": "af_heart",
            "scalar": 0.3,
        },
    ]
    out = {e["engine_id"]: e for e in d._validate_per_engine(raw, engines, 0.5, _SOURCE)}

    assert out["breeze"]["instruction"] == "Slow, restrained, holding the last word."
    assert out["breeze"]["text"].startswith("(sigh)")
    assert out["kokoro"]["instruction"] is None
    assert out["kokoro"]["text"] is None
    assert out["kokoro"]["voice"] == "af_heart"  # real capabilities still honoured


def test_missing_engines_still_get_a_complete_entry() -> None:
    engines = [DirectorEngineSpec(id="breeze", instruction=True, audio_tags=_TAGS)]
    out = d._validate_per_engine([], engines, 0.4, _SOURCE)
    assert out[0] == {
        "engine_id": "breeze",
        "emotion_label": None,
        "voice": None,
        "scalar": 0.4,
        "instruction": None,
        "text": None,
        "rejected_text": None,
        "text_status": "none",
        "text_reason": None,
    }


# ---- the prompt file --------------------------------------------------------

def test_prompt_body_is_loaded_and_the_editor_header_is_stripped() -> None:
    body = d.load_prompt_template()
    assert body.startswith("You are an audiobook casting director")
    assert "Loaded at request time" not in body  # the docs above `---` never ship


def test_prompt_substitutes_engine_capabilities() -> None:
    req = DirectorRequest(
        text=_SOURCE,
        model="gemma3:12b",
        engines=[
            DirectorEngineSpec(id="breeze", name="Breeze", instruction=True, audio_tags=_TAGS),
            DirectorEngineSpec(id="kokoro", name="Kokoro", voices=["af_heart"]),
        ],
    )
    prompt = d.build_prompt(req)
    assert "{{TEXT}}" not in prompt and "{{ENGINE_BLOCK}}" not in prompt
    assert _SOURCE in prompt
    assert 'id "breeze" (Breeze)' in prompt
    assert "instruction = yes" in prompt
    assert "(sigh)" in prompt
    # kokoro must be told it has neither, so the model doesn't offer them.
    kokoro_line = next(ln for ln in prompt.splitlines() if 'id "kokoro"' in ln)
    assert "instruction = no" in kokoro_line
    assert "audio tags = none" in kokoro_line


def test_prompt_reloads_when_the_file_changes(tmp_path, monkeypatch) -> None:
    """Prompt tuning shouldn't need a restart — that's the whole point of the file."""
    variant = tmp_path / "director.md"
    variant.write_text("docs\n---\nVERSION ONE {{TEXT}}", encoding="utf-8")
    monkeypatch.setenv("DIRECTOR_PROMPT_PATH", str(variant))
    monkeypatch.setattr(d, "_prompt_cache", None)
    assert d.load_prompt_template() == "VERSION ONE {{TEXT}}"

    import os
    variant.write_text("docs\n---\nVERSION TWO {{TEXT}}", encoding="utf-8")
    os.utime(variant, (0, 0))  # force a distinct mtime, not just a fast rewrite
    assert d.load_prompt_template() == "VERSION TWO {{TEXT}}"


# ---- config-driven director model -------------------------------------------

def test_defaults_endpoint_reports_the_configured_model(monkeypatch) -> None:
    """The UI reads its default from here, so moving to a bigger box is one env
    var — not a frontend rebuild."""
    from gateway.app import config

    monkeypatch.setenv("DIRECTOR_MODEL", "qwen3:30b")
    config.get_settings.cache_clear()
    try:
        body = TestClient(create_app()).get("/api/director/defaults").json()
        assert body["provider"] == "ollama"
        assert body["model"] == "qwen3:30b"
    finally:
        config.get_settings.cache_clear()


# ---- scene note + pace --------------------------

def _parsed(**fields) -> dict:
    import json as _json

    return d._parse(_json.dumps({"summary": "s", "per_engine": [], **fields}))


def test_a_scene_note_and_a_pace_survive_parsing() -> None:
    data = _parsed(scene_note="  Elara and Marcus,\n both avoiding it. ", pace=0.9)
    assert data["scene_note"] == "Elara and Marcus, both avoiding it."
    assert data["pace"] == 0.9


def test_pace_is_clamped_into_the_band_every_engine_can_honour() -> None:
    """Clamped rather than dropped: a model answering 1.6 wants it faster, and
    throwing the field away loses that. 0.8-1.2 is the band a reading survives."""
    assert _parsed(pace=1.6)["pace"] == 1.2
    assert _parsed(pace=0.2)["pace"] == 0.8
    assert _parsed(pace=1.0)["pace"] == 1.0


def test_an_unusable_pace_becomes_none_not_a_default() -> None:
    """"No pace asked for" and "read it at 1.0" are different answers."""
    assert _parsed(pace="quickly")["pace"] is None
    assert _parsed(pace=None)["pace"] is None
    assert _parsed()["pace"] is None
    assert _parsed(scene_note="null")["scene_note"] == ""


def test_a_rambling_scene_note_is_truncated() -> None:
    assert len(_parsed(scene_note="word " * 400)["scene_note"]) <= d._MAX_INSTRUCTION_CHARS


def test_the_director_result_accepts_the_new_fields() -> None:
    from gateway.app.models import DirectorResult

    result = DirectorResult(**_parsed(scene_note="Two people, one lie.", pace=0.85))
    assert result.scene_note == "Two people, one lie."
    assert result.pace == 0.85
    assert DirectorResult().pace is None and DirectorResult().scene_note == ""


def test_the_prompt_asks_for_a_line_note_not_a_scene_summary() -> None:
    """F13: the instruction is stamped verbatim on every line of the scene, so
    the prompt has to ask for something that reads correctly as one line's note."""
    body = d.load_prompt_template()
    assert '"scene_note"' in body and '"pace"' in body
    assert "0.8" in body and "1.2" in body
    assert "Write it to the performer of a single line" in body
    assert "An exchange between Elara and Marcus" in body  # named as the bad example
