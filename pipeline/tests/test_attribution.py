"""Speaker attribution + casting tests. Deterministic, offline."""

from __future__ import annotations

from pipeline.attribute import Attributer, split_quotes
from pipeline.build import build_book_script
from pipeline.cast import FEMALE_VOICES, MALE_VOICES, assign_voices
from pipeline.schema import UtteranceKind


def _speakers(turns):
    return [(t.speaker, t.kind) for t in turns]


def test_split_quotes_alternates():
    spans = split_quotes('The door creaked. "Who\'s there?" she called.')
    assert [s[1] for s in spans] == [False, True, False]
    assert spans[1][0] == '"Who\'s there?"'


def test_attribute_said_name_after_and_before():
    a = Attributer()
    assert a.attribute('"Hello," said Elara.')[0].speaker == "Elara"
    b = Attributer()
    turns = b.attribute('Marcus said, "Goodbye now."')
    quote = [t for t in turns if t.kind == "dialogue"][0]
    assert quote.speaker == "Marcus"


def test_attribute_mixed_narration_and_dialogue():
    turns = Attributer().attribute('The door creaked. "Who\'s there?" Elara whispered.')
    kinds = _speakers(turns)
    assert ("Narrator", "narration") in kinds
    assert ("Elara", "dialogue") in kinds


def test_attribute_pronoun_uses_recent_name():
    a = Attributer()
    a.attribute("Elara opened the door.")  # establishes the antecedent
    turns = a.attribute('"Careful," she warned.')
    assert turns[0].speaker == "Elara"
    assert a.ctx.characters["Elara"] == "f"


def test_attribute_continuation_within_paragraph():
    turns = Attributer().attribute('"Only me," said Marcus. He stepped forward. "You\'re late."')
    quotes = [t for t in turns if t.kind == "dialogue"]
    assert quotes[0].speaker == "Marcus"
    assert quotes[1].speaker == "Marcus"  # same speaker continues, not alternation


def test_attribute_alternation_abstains_instead_of_guessing():
    """Changed deliberately. Alternation used to answer
    "Elara" with the same confidence as an explicit tag; on ordinary dialogue
    that put five of ten attributable lines in the wrong character's mouth, and a
    wrong speaker is invisible in the finished audio. It now abstains, and says
    on what basis it was abstaining."""
    a = Attributer()
    a.attribute('"Hi," said Elara.')
    a.attribute('"Hello," said Marcus.')
    turn = a.attribute('"Indeed."')[0]
    assert turn.speaker == "Unknown"
    assert (turn.basis, turn.confidence) == ("alternation", 0.4)


def test_a_tag_is_stated_with_high_confidence():
    turn = Attributer().attribute('"Hello," said Elara.')[0]
    assert (turn.speaker, turn.basis) == ("Elara", "tag")
    assert turn.confidence >= 0.9


def test_narration_is_never_a_guess():
    turns = Attributer().attribute("The door creaked open.")
    assert (turns[0].basis, turns[0].confidence) == ("none", 1.0)


def test_an_action_beat_is_not_a_speech_tag():
    """`Alex shrugged. "Nobody saw him leave."` The
    beat names Alex but does not attribute the line, so the old alternation
    fallback handed it to whoever spoke last."""
    a = Attributer()
    a.attribute('"Someone has been careless," said Ren.')
    turn = a.attribute('Alex shrugged. "Nobody saw him leave."')[1]
    assert turn.speaker != "Ren"
    assert turn.speaker == "Unknown"


def test_a_quote_with_no_tag_at_all_is_unknown():
    """`"Am I?" The voice came from the doorway.` — deliberately unknowable."""
    a = Attributer()
    a.attribute('"Sit down," said Vasquez.')
    assert a.attribute('"Am I?" The voice came from the doorway.')[0].speaker == "Unknown"


def test_reset_stops_a_speaker_leaking_across_a_chapter_boundary():
    """The chapter-three leak: `"It is finished,"` opened a new chapter and was
    voiced as the character who spoke last in chapter two."""
    a = Attributer()
    a.attribute('"It is finished," said Yusuf.')
    a.reset()
    turn = a.attribute('"It is finished," she said, and no one answered her.')[0]
    assert turn.speaker != "Yusuf"
    assert turn.speaker == "Unknown"


def test_reset_keeps_the_gender_it_learned():
    """Casting reads ``ctx.characters`` after the whole book is built, so the
    reset must forget *position*, not what a character is."""
    a = Attributer()
    a.attribute('"Yes," said Elara.')
    a.attribute("Elara closed her book.")
    a.reset()
    assert a.ctx.characters.get("Elara") == "f"
    assert a.ctx.last_female is None and a.ctx.recent == []


def test_pronoun_does_not_flip_known_gender():
    a = Attributer()
    a.attribute('"Yes," said Elara.')  # Elara
    a.ctx.characters["Elara"] = "f"
    # a stray "he" must not resolve to the female Elara
    assert a.ctx.resolve("he")[0] != "Elara"


def test_casting_is_gendered_and_distinct():
    cast = assign_voices(["Elara", "Marcus", "Nadia"], {"Elara": "f", "Marcus": "m", "Nadia": "f"}, "af_heart")
    assert cast["Narrator"] == "af_heart"
    assert cast["Elara"] in FEMALE_VOICES and cast["Elara"] != "af_heart"
    assert cast["Marcus"] in MALE_VOICES
    assert cast["Nadia"] in FEMALE_VOICES
    assert len({cast["Elara"], cast["Marcus"], cast["Nadia"]}) == 3  # all distinct


DIALOGUE = """Chapter One

Elara pushed open the heavy door. "Is anyone there?" she called.

"Only me," said Marcus. He stepped out of the shadows. "You're late."

"The roads were flooded," Elara replied."""


def test_build_multi_voice_casts_characters(tmp_path):
    p = tmp_path / "d.txt"
    p.write_text(DIALOGUE, encoding="utf-8")
    script = build_book_script(str(p), book_type="general_fiction")

    names = {c.character for c in script.cast}
    assert names == {"Narrator", "Elara", "Marcus"}
    voices = {c.character: c.voice_id for c in script.cast}
    assert voices["Elara"] in FEMALE_VOICES
    assert voices["Marcus"] in MALE_VOICES
    assert len({voices["Narrator"], voices["Elara"], voices["Marcus"]}) == 3

    # dialogue utterances carry the character's voice, not the narrator's.
    elara = [u for u in script.utterances if u.speaker == "Elara"]
    assert elara and all(u.kind == UtteranceKind.dialogue for u in elara)
    assert all(u.render.voice_id == voices["Elara"] for u in elara)


def test_single_voice_override(tmp_path):
    p = tmp_path / "d.txt"
    p.write_text(DIALOGUE, encoding="utf-8")
    script = build_book_script(str(p), book_type="general_fiction", multi_voice=False)
    assert len(script.cast) == 1
    assert all(u.speaker == "Narrator" for u in script.utterances)


def test_pronoun_with_two_unknown_gender_names_abstains():
    """Mara is mentioned in narration, Tomas speaks with a
    tag, then '"Eventually," she said.' The old resolver adopted the most recent
    name (Tomas) as female and voiced Mara's line in his voice. With two
    unknown-gender names in play the pronoun is a guess, so the line abstains."""
    from pipeline.attribute import Attributer

    a = Attributer()
    a.attribute("The letter arrived on a Tuesday. Mara read it twice and put it in the drawer.")
    tagged = a.attribute('"Are you going to answer it?" asked Tomas from the doorway.')
    assert tagged[0].speaker == "Tomas" and tagged[0].basis == "tag"
    turns = a.attribute('"Eventually," she said. "Not today."')
    quotes = [t for t in turns if t.kind == "dialogue"]
    assert quotes[0].speaker == "Unknown"
    assert quotes[0].basis == "pronoun" and quotes[0].confidence < 0.5
    assert a.ctx.characters.get("Tomas", "n") != "f"


def test_pronoun_with_one_name_still_resolves():
    from pipeline.attribute import Attributer

    a = Attributer()
    a.attribute('"Late again," said Elara.')
    turns = a.attribute('"I know," she said.')
    quotes = [t for t in turns if t.kind == "dialogue"]
    assert quotes[0].speaker == "Elara" and quotes[0].confidence >= 0.5
