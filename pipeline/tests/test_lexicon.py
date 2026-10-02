"""Pronunciation lexicon tests. Deterministic, offline."""

from __future__ import annotations

from pipeline.build import build_book_script
from pipeline.lexicon import (
    apply_lexicon,
    lexicon_terms_in,
    load_lexicon,
    make_text_transform,
    save_lexicon,
)
from pipeline.render import Renderer, fake_synth

LEXICON = {"Elara": "eh-LAR-uh", "Hermione": "her-MY-oh-nee"}


# ---- apply_lexicon: matching rules -----------------------------------------


def test_apply_lexicon_replaces_whole_word_case_insensitively():
    assert apply_lexicon("elara opened the door.", LEXICON) == "eh-LAR-uh opened the door."
    assert apply_lexicon("ELARA opened the door.", LEXICON) == "eh-LAR-uh opened the door."


def test_apply_lexicon_never_matches_inside_another_word():
    text = "Elaraville was a small town."
    assert apply_lexicon(text, LEXICON) == text


def test_apply_lexicon_longest_key_first():
    lex = {"Elara": "eh-LAR-uh", "Elara Vance": "eh-LAR-uh VANSS"}
    assert apply_lexicon("Elara Vance arrived.", lex) == "eh-LAR-uh VANSS arrived."
    assert apply_lexicon("Elara arrived.", lex) == "eh-LAR-uh arrived."


def test_apply_lexicon_possessive_keeps_the_s():
    assert apply_lexicon("Elara's sister waited.", LEXICON) == "eh-LAR-uh's sister waited."


def test_apply_lexicon_multiple_distinct_terms():
    text = "Elara found Hermione in the library."
    assert apply_lexicon(text, LEXICON) == "eh-LAR-uh found her-MY-oh-nee in the library."


def test_apply_lexicon_never_touches_bracketed_tags():
    text = "(Elara) Elara said hello."
    # The tag "(Elara)" is left alone; the real word outside it is respelled.
    assert apply_lexicon(text, LEXICON) == "(Elara) eh-LAR-uh said hello."


def test_apply_lexicon_skips_a_generic_vocal_tag_containing_other_text():
    text = "(sigh) It was over."
    assert apply_lexicon(text, {"sigh": "SY"}) == "(sigh) It was over."


def test_apply_lexicon_capitalizes_a_lowercase_authored_key_at_a_capital_occurrence():
    # A key authored lowercase (an ordinary word, not a proper noun) that only
    # reads capitalized because it opened a sentence gets its respelling
    # capitalized to match, so the sentence still reads naturally.
    lex = {"quinoa": "keen-wah"}
    assert apply_lexicon("Quinoa is a great source of protein.", lex) == "Keen-wah is a great source of protein."
    assert apply_lexicon("I like quinoa.", lex) == "I like keen-wah."


def test_apply_lexicon_proper_noun_key_keeps_authored_case_regardless_of_occurrence():
    # A key authored capitalized (a name) is used verbatim — the author chose
    # to write the respelling lowercase on purpose (see the possessive test).
    assert apply_lexicon("Elara arrived.", LEXICON).startswith("eh-LAR-uh")


def test_apply_lexicon_empty_lexicon_is_a_no_op():
    text = "Elara said hello."
    assert apply_lexicon(text, {}) == text


# ---- lexicon_terms_in -------------------------------------------------------


def test_lexicon_terms_in_reports_which_keys_occur():
    text = "Elara found Marcus, not Hermione."
    assert lexicon_terms_in(text, LEXICON) == ["Elara", "Hermione"]


def test_lexicon_terms_in_empty_when_nothing_matches():
    assert lexicon_terms_in("Marcus walked away.", LEXICON) == []


def test_lexicon_terms_in_empty_lexicon():
    assert lexicon_terms_in("Elara said hello.", {}) == []


# ---- load_lexicon / save_lexicon --------------------------------------------


def test_load_lexicon_merges_in_order_later_wins(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text('{"Elara": "eh-LAR-uh", "Marcus": "MAR-kus"}', encoding="utf-8")
    b.write_text('{"Elara": "el-AR-a"}', encoding="utf-8")  # overrides a's Elara

    merged = load_lexicon(str(a), str(b))
    assert merged == {"Elara": "el-AR-a", "Marcus": "MAR-kus"}


def test_load_lexicon_ignores_missing_files(tmp_path):
    a = tmp_path / "a.json"
    a.write_text('{"Elara": "eh-LAR-uh"}', encoding="utf-8")
    missing = tmp_path / "does-not-exist.json"

    merged = load_lexicon(str(missing), str(a), "")
    assert merged == {"Elara": "eh-LAR-uh"}


def test_load_lexicon_no_paths_returns_empty_dict():
    assert load_lexicon() == {}


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "sub" / "lexicon.json"
    save_lexicon(str(path), LEXICON)
    assert load_lexicon(str(path)) == LEXICON


# ---- make_text_transform ----------------------------------------------------


def test_make_text_transform_applies_the_bound_lexicon():
    transform = make_text_transform(LEXICON)
    assert transform("Elara said hello.") == "eh-LAR-uh said hello."


# ---- text_transform changes the render cache key, only for affected lines --

DIALOGUE = (
    "Chapter One\n\n"
    'Elara pushed open the heavy door. "Is anyone there?" she called.\n\n'
    '"Only me," said Marcus. He stepped out of the shadows. "You\'re late."\n'
)


def test_text_transform_changes_the_cache_key_only_for_affected_lines(tmp_path):
    """The point of applying text_transform before the cache key: a lexicon
    change re-renders exactly the lines that mention the changed term, and
    leaves every other line's cache untouched."""
    p = tmp_path / "d.txt"
    p.write_text(DIALOGUE, encoding="utf-8")

    script1 = build_book_script(str(p), book_type="general_fiction")
    out1 = tmp_path / "out1"
    Renderer(fake_synth(), cache_dir=str(out1 / ".cache")).render(script1, str(out1))
    keys1 = {u.id: u.audio.cache_key for u in script1.utterances}

    script2 = build_book_script(str(p), book_type="general_fiction")  # fresh, identical parse
    lexicon = {"Elara": "eh-LAR-uh"}
    out2 = tmp_path / "out2"
    Renderer(
        fake_synth(), cache_dir=str(out2 / ".cache"), text_transform=make_text_transform(lexicon)
    ).render(script2, str(out2))
    keys2 = {u.id: u.audio.cache_key for u in script2.utterances}

    affected = {u.id for u in script1.utterances if "elara" in u.text.lower()}
    assert affected, "fixture sanity: at least one line must mention Elara"
    assert keys1.keys() == keys2.keys()

    for uid in keys1:
        if uid in affected:
            assert keys1[uid] != keys2[uid], f"{uid} mentions Elara and should have re-rendered"
        else:
            assert keys1[uid] == keys2[uid], f"{uid} does not mention Elara and should be unaffected"


def test_no_text_transform_is_a_no_op_on_the_cache_key(tmp_path):
    p = tmp_path / "d.txt"
    p.write_text(DIALOGUE, encoding="utf-8")

    script1 = build_book_script(str(p), book_type="general_fiction")
    out1 = tmp_path / "out1"
    Renderer(fake_synth(), cache_dir=str(out1 / ".cache")).render(script1, str(out1))

    script2 = build_book_script(str(p), book_type="general_fiction")
    out2 = tmp_path / "out2"
    Renderer(fake_synth(), cache_dir=str(out2 / ".cache"), text_transform=None).render(script2, str(out2))

    keys1 = {u.id: u.audio.cache_key for u in script1.utterances}
    keys2 = {u.id: u.audio.cache_key for u in script2.utterances}
    assert keys1 == keys2


def test_keys_ending_in_punctuation_match():
    """"Dr." and "C++" end in non-word characters, which a word-boundary anchor
    can never sit next to; the lookaround form matches them and still refuses
    "Elaraville"."""
    from pipeline.lexicon import apply_lexicon, lexicon_terms_in

    lex = {"Dr.": "Doctor", "C++": "C plus plus", "Elara": "eh-LAR-uh"}
    text = "She thought about Dr. Smith and the C++ code; Elara went to Elaraville."
    assert apply_lexicon(text, lex) == (
        "She thought about Doctor Smith and the C plus plus code; eh-LAR-uh went to Elaraville."
    )
    assert sorted(lexicon_terms_in(text, lex)) == ["C++", "Dr.", "Elara"]
