"""Pipeline tests — pure, no models/network. Run: pytest pipeline/tests"""

from __future__ import annotations

from pipeline.build import TAG_GAP_MS, build_book_script
from pipeline.chunk import CHAPTER_PAUSE_MS, SCENE_PAUSE_MS, split_sentences
from pipeline.clean import clean_paragraph
from pipeline.ingest import ingest_txt
from pipeline.profiles import get_profile
from pipeline.schema import BookScript, UtteranceKind


def test_split_respects_abbreviations() -> None:
    assert split_sentences("Dr. Vane closed the door. He waited.") == [
        "Dr. Vane closed the door.",
        "He waited.",
    ]


def test_split_does_not_break_on_decimals() -> None:
    # "3.14" must not become two sentences.
    assert split_sentences("It was 3.14 exactly. Fine.") == ["It was 3.14 exactly.", "Fine."]


def test_split_keeps_dialogue_terminator_with_quote() -> None:
    out = split_sentences('"Run!" she cried. It was late.')
    assert out == ['"Run!" she cried.', "It was late."]


def test_build_detects_two_chapters(tmp_path) -> None:
    p = tmp_path / "b.txt"
    p.write_text(
        "Chapter One\n\nThe sea was calm. Then it was not.\n\nChapter Two\n\nHe left.",
        encoding="utf-8",
    )
    script = build_book_script(str(p))
    assert len(script.chapters) == 2
    assert script.chapters[0].title == "Chapter One"
    assert script.utterances[0].chapter_index == 0
    assert script.utterances[-1].chapter_index == 1


def test_heading_guard_ignores_prose_starting_with_chapter(tmp_path) -> None:
    p = tmp_path / "b.txt"
    p.write_text("Chapter one of my life was the hardest part of all.", encoding="utf-8")
    script = build_book_script(str(p))
    # That line is prose, not a heading — it must not create an empty titled chapter.
    assert len(script.chapters) == 1
    assert script.utterances[0].text.startswith("Chapter one of my life")


def test_scene_break_creates_second_scene(tmp_path) -> None:
    p = tmp_path / "b.txt"
    p.write_text("Chapter One\n\nAlpha here.\n\n* * *\n\nBravo there.", encoding="utf-8")
    script = build_book_script(str(p))
    scenes = {u.scene_index for u in script.utterances if u.chapter_index == 0}
    assert scenes == {0, 1}
    # Utterance right before the scene break carries the scene-length pause.
    before_break = [u for u in script.utterances if u.scene_index == 0][-1]
    assert before_break.render.trail_pause_ms == SCENE_PAUSE_MS


def test_chapter_final_utterance_gets_chapter_pause(tmp_path) -> None:
    p = tmp_path / "b.txt"
    p.write_text("Chapter One\n\nOnly line here.\n\nChapter Two\n\nNext.", encoding="utf-8")
    script = build_book_script(str(p))
    last_of_ch0 = [u for u in script.utterances if u.chapter_index == 0][-1]
    assert last_of_ch0.render.trail_pause_ms == CHAPTER_PAUSE_MS


def test_book_script_round_trips_through_json(tmp_path) -> None:
    p = tmp_path / "b.txt"
    p.write_text("Chapter One\n\nHello there. General Kenobi.", encoding="utf-8")
    script = build_book_script(str(p), voice_id="af_heart")
    restored = BookScript.model_validate_json(script.model_dump_json())
    assert restored == script
    assert restored.cast[0].voice_id == "af_heart"


# ---- F8: the sentence splitter breaks real prose, in both directions --------

def test_split_guards_the_publishing_abbreviations() -> None:
    """`Ch.` used to end a sentence mid-parenthesis: the narrator read
    "…(see Ch." then paused for 600 ms then said "4)."."""
    assert split_sentences("The St. Mary's account (see Ch. 4). It balanced.") == [
        "The St. Mary's account (see Ch. 4).",
        "It balanced.",
    ]
    assert split_sentences("She read Mr. Vane, vol. 2, pp. 20-24. It helped.") == [
        "She read Mr. Vane, vol. 2, pp. 20-24.",
        "It helped.",
    ]


def test_a_footnote_marker_on_the_period_is_part_of_the_boundary() -> None:
    out = split_sentences("He had reasons to lie.[1] The ledger showed otherwise.")
    assert out == ["He had reasons to lie.[1]", "The ledger showed otherwise."]


def test_a_bare_enumerator_never_stands_as_its_own_utterance() -> None:
    """Otherwise the narrator says "One." and pauses before reading the note."""
    assert split_sentences("1. The ledger has since been lost.") == [
        "1. The ledger has since been lost."
    ]
    assert split_sentences("IV. The reckoning came late.") == [
        "IV. The reckoning came late."
    ]


def test_the_splitter_does_not_break_inside_parentheses_or_quotes() -> None:
    assert split_sentences("He paused (it was late. Very late) and left. Then dawn.") == [
        "He paused (it was late. Very late) and left.",
        "Then dawn.",
    ]
    assert split_sentences('"In this storm? No ship will ever see us." He turned.') == [
        '"In this storm? No ship will ever see us."',
        "He turned.",
    ]


def test_an_unclosed_quote_still_splits() -> None:
    """A speech that runs on into the next paragraph opens a quote it never
    closes; suppressing every boundary in it would weld the paragraph together."""
    assert len(split_sentences('"The roads were flooded. We turned back.')) == 2


def test_footnote_markers_are_stripped_for_every_profile() -> None:
    fiction = get_profile("general_fiction")
    assert fiction.strip_footnote_markers is False  # the profile lever is untouched
    assert clean_paragraph("He had reasons to lie.[1] The ledger lied too.", fiction) == (
        "He had reasons to lie. The ledger lied too."
    )
    assert clean_paragraph("Bridget listened without moving.\u00b2", fiction) == (
        "Bridget listened without moving."
    )


# ---- F6: chapter headings are spoken ---------------------------------------

def test_headings_are_spoken_when_the_profile_asks(tmp_path) -> None:
    p = tmp_path / "b.txt"
    p.write_text("Chapter One: The Lighthouse\n\nRain hammered the windows.", encoding="utf-8")
    script = build_book_script(str(p), book_type="general_fiction")

    heading = script.utterances[0]
    assert heading.kind == UtteranceKind.heading
    assert heading.text == "Chapter One: The Lighthouse"
    assert heading.speaker == "Narrator"
    assert heading.render.trail_pause_ms == script.profile.heading_pause_ms


def test_read_headings_off_speaks_no_heading(tmp_path) -> None:
    p = tmp_path / "b.txt"
    p.write_text("Chapter One\n\nRain hammered the windows.", encoding="utf-8")
    profile = get_profile("general_fiction")
    profile.read_headings = False
    script = build_book_script(str(p), profile=profile)
    assert all(u.kind != UtteranceKind.heading for u in script.utterances)


def test_an_untitled_chapter_gets_no_spoken_heading(tmp_path) -> None:
    """`Chapter.title` falls back to "Chapter N" for display; announcing a title
    the source never had would put words in the book."""
    p = tmp_path / "b.txt"
    p.write_text("Rain hammered the windows. It was late.", encoding="utf-8")
    script = build_book_script(str(p), book_type="general_fiction")
    assert script.chapters[0].title == "Chapter 1"
    assert all(u.kind != UtteranceKind.heading for u in script.utterances)


# ---- F5: the dialogue-tag gap ----------------------------------------------

def test_a_dialogue_tag_closes_up_against_its_quote(tmp_path) -> None:
    """`"You let it go out," she said.` is one breath group — it used to render
    as the character's voice, 350 ms of silence, then the narrator."""
    script = build_book_script("pipeline/samples/dialogue.txt", book_type="general_fiction")
    quote = next(u for u in script.utterances if u.text.startswith('"You let it go out'))
    tag = script.utterances[quote.order + 1]

    assert tag.text == "she said."
    assert quote.render.trail_pause_ms == TAG_GAP_MS == 80
    assert tag.render.lead_pause_ms == 0


def test_a_tag_between_two_quotes_from_one_speaker_closes_on_both_sides() -> None:
    script = build_book_script("pipeline/samples/dialogue.txt", book_type="general_fiction")
    tag = next(u for u in script.utterances if u.text == "he answered.")
    following = script.utterances[tag.order + 1]

    assert script.utterances[tag.order - 1].render.trail_pause_ms == TAG_GAP_MS
    assert tag.render.trail_pause_ms == TAG_GAP_MS
    assert following.speaker == script.utterances[tag.order - 1].speaker


def test_narration_that_is_not_a_tag_keeps_its_full_pause() -> None:
    script = build_book_script("pipeline/samples/dialogue.txt", book_type="general_fiction")
    beat = next(u for u in script.utterances if u.text.startswith("Marcus did not look up"))
    assert beat.render.trail_pause_ms > TAG_GAP_MS


# ---- F11: verse inside a prose book ----------------------------------------

VERSE_IN_A_NOVEL = """Chapter Two

Kai read the four lines aloud.

    The tide came in without a sound
    And took the lantern from the shore
    It left no footprint on the ground
    And yet the light was there no more

Bridget said nothing at all.
"""


def test_an_indented_short_line_block_is_verse_under_any_profile(tmp_path) -> None:
    p = tmp_path / "novel.txt"
    p.write_text(VERSE_IN_A_NOVEL, encoding="utf-8")
    script = build_book_script(str(p), book_type="general_fiction")

    verse = [u for u in script.utterances if u.kind == UtteranceKind.verse]
    assert [u.text for u in verse] == [
        "The tide came in without a sound",
        "And took the lantern from the shore",
        "It left no footprint on the ground",
        "And yet the light was there no more",
    ]
    assert [u.render.trail_pause_ms for u in verse[:3]] == [script.profile.line_pause_ms] * 3


def test_ordinary_wrapped_prose_is_not_mistaken_for_verse(tmp_path) -> None:
    p = tmp_path / "novel.txt"
    p.write_text(
        "Chapter One\n\nThe storm had been promised for three days.\nOn the third it "
        "finally came.\nDr. Vane climbed the stairs.\n",
        encoding="utf-8",
    )
    script = build_book_script(str(p), book_type="general_fiction")
    assert all(u.kind != UtteranceKind.verse for u in script.utterances)


# ---- F9: front matter is silently narrated as Chapter 1 --------------------

MANUSCRIPT_SHAPE = """Copyright 2026 by A. Producer.

All rights reserved. ISBN 978-0-00-000000-0.

Dedication

For everyone who ever waited up.

Chapter One

The storm had been promised for three days.

About the Author

A. Producer has narrated two hundred audiobooks.
"""


def _by_title(script):
    return {c.title: c for c in script.chapters}


def test_front_matter_is_marked_and_dropped_from_the_audio(tmp_path) -> None:
    p = tmp_path / "m.txt"
    p.write_text(MANUSCRIPT_SHAPE, encoding="utf-8")
    script = build_book_script(str(p), book_type="general_fiction")

    copyright_page = script.chapters[0]
    assert copyright_page.kind == "front_matter"
    assert copyright_page.include_in_audio is False
    assert _by_title(script)["Dedication"].kind == "front_matter"
    assert _by_title(script)["Chapter One"].kind == "chapter"


def test_back_matter_is_marked_but_stays_in_the_audio(tmp_path) -> None:
    p = tmp_path / "m.txt"
    p.write_text(MANUSCRIPT_SHAPE, encoding="utf-8")
    script = build_book_script(str(p), book_type="general_fiction")
    about = _by_title(script)["About the Author"]
    assert about.kind == "back_matter"
    assert about.include_in_audio is True


def test_excluded_front_matter_is_not_announced(tmp_path) -> None:
    p = tmp_path / "m.txt"
    p.write_text(MANUSCRIPT_SHAPE, encoding="utf-8")
    script = build_book_script(str(p), book_type="general_fiction")
    spoken_headings = {u.text for u in script.utterances if u.kind == UtteranceKind.heading}
    assert "Dedication" not in spoken_headings
    assert "Chapter One" in spoken_headings


def test_chapter_numbering_does_not_count_front_matter(monkeypatch, tmp_path) -> None:
    """The whole book's numbering used to be off by one, permanently, including
    in the exported chapter markers."""
    from pipeline import build as build_module
    from pipeline.ingest import RawBook, RawChapter, mark_matter

    raw = RawBook(
        chapters=[
            RawChapter(paragraphs=["Copyright 2026 by A. Producer. All rights reserved."]),
            RawChapter(paragraphs=["The storm had been promised for three days."]),
            RawChapter(paragraphs=["They left the island on the first clear tide."]),
        ]
    )
    mark_matter(raw)
    monkeypatch.setattr(build_module, "ingest_book", lambda path: raw)
    source = tmp_path / "m.txt"
    source.write_text("(ingest is stubbed above)", encoding="utf-8")
    script = build_book_script(str(source), book_type="general_fiction")

    assert [c.kind for c in script.chapters] == ["front_matter", "chapter", "chapter"]
    assert [c.title for c in script.chapters] == ["", "Chapter 1", "Chapter 2"]


def test_the_only_chapter_is_never_labelled_front_matter(tmp_path) -> None:
    """A short first chapter with nothing after it is the book, not its imprint
    page — labelling it would silence the whole title."""
    p = tmp_path / "m.txt"
    p.write_text("It was over before it began.", encoding="utf-8")
    script = build_book_script(str(p), book_type="general_fiction")
    assert script.chapters[0].kind == "chapter"
    assert script.chapters[0].include_in_audio is True


def test_a_real_first_chapter_survives_the_heuristic(tmp_path) -> None:
    p = tmp_path / "m.txt"
    p.write_text(
        "Chapter One\n\nThe storm had been promised for three days.\n\n"
        "Chapter Two\n\nThey left the island.",
        encoding="utf-8",
    )
    raw = ingest_txt(str(p))
    assert [c.kind for c in raw.chapters] == ["chapter", "chapter"]


# ---- the hard manuscript, end to end ---------------------------------------

def test_the_hard_manuscript_survives_every_trap() -> None:
    """One pass over `samples/hard-manuscript.txt`, which carries every
    ingest and attribution trap found so far in one file."""
    script = build_book_script(
        "pipeline/samples/hard-manuscript.txt", book_type="general_fiction"
    )
    text = " ".join(u.text for u in script.utterances)
    by_order = {u.order: u for u in script.utterances}

    # F9 front matter out, back matter in, numbering intact.
    kinds = [c.kind for c in script.chapters]
    assert kinds[:2] == ["front_matter", "front_matter"] and kinds[-1] == "back_matter"
    assert not any(u.chapter_index in (0, 1) and u.kind == UtteranceKind.heading
                   for u in script.utterances)

    # F6 every real chapter announces itself.
    headings = [u.text for u in script.utterances if u.kind == UtteranceKind.heading]
    assert headings == [
        "Chapter One: The Ledger",
        "Chapter Two: The Cold Lantern",
        "Chapter Three: After",
        "About the Author",
    ]

    # F8 no marker reaches the engine; no sentence broke inside the parenthesis;
    # the footnote body kept its enumerator.
    assert "[1]" not in text and "\u00b2" not in text
    assert "(see Ch. 4)." in text
    # The footnote *body* is apparatus too: a numbered non-sequitur read mid-scene.
    assert "The ledger has since been lost." not in text

    # F1 the four action-beat quotes abstain instead of naming the last speaker.
    unattributed = [u for u in script.utterances if u.speaker == "Unknown"]
    assert len(unattributed) >= 4
    assert all(u.attribution.confidence < 0.5 for u in unattributed)
    assert "Ren" not in {u.speaker for u in unattributed}
    doorway = next(u for u in script.utterances if u.text == '"Am I?"')
    assert doorway.speaker == "Unknown"

    # F1 the chapter-three tag did not inherit chapter two's speaker.
    chapter_three = [u for u in script.utterances if u.chapter_index == 4]
    first_quote = next(u for u in chapter_three if u.kind == UtteranceKind.dialogue)
    assert first_quote.speaker != "Yusuf"

    # F1 an explicit tag is still stated, and casting still knows the genders.
    ren = next(u for u in script.utterances if u.speaker == "Ren")
    assert (ren.attribution.basis, ren.attribution.confidence) == ("tag", 0.95)
    assert {"Ren", "Vasquez", "Yusuf", "Bridget", "Kai", "Unknown"} <= {
        c.character for c in script.cast
    }

    # F5 every dialogue tag closes up against its quote.
    tags = [u for u in script.utterances if u.text in ("said Ren.", "Alex said.", "Kai said.")]
    assert tags
    assert all(by_order[u.order - 1].render.trail_pause_ms == TAG_GAP_MS for u in tags)

    # F11 the poem kept its line breaks.
    verse = [u for u in script.utterances if u.kind == UtteranceKind.verse]
    assert len(verse) == 4


def test_footnote_bodies_and_unknown_voice(tmp_path):
    from pipeline.build import build_book_script

    src = tmp_path / "fn.txt"
    src.write_text(
        "Chapter One\n\nThe ledger was gone.[1] Nobody said so.\n\n"
        '"Who took it?" asked Mara.\n\n"Nobody saw."\n\n1. The ledger has since been lost.\n',
        encoding="utf-8",
    )
    script = build_book_script(str(src), book_type="general_fiction", multi_voice=True)
    texts = [u.text for u in script.utterances]
    assert not any(t.startswith("1.") for t in texts), texts
    assert "[1]" not in " ".join(texts)
    narrator = next(c for c in script.cast if c.character == "Narrator")
    unknown = [c for c in script.cast if c.character == "Unknown"]
    if unknown:
        assert unknown[0].voice_id == narrator.voice_id
        for u in script.utterances:
            if u.speaker == "Unknown":
                assert u.render.voice_id == narrator.voice_id
