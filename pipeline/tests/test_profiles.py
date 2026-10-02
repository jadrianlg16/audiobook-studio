"""Genre-awareness tests: classification, skip-rules cleaning, verse chunking,
and profile-driven build output. Fully offline (heuristic classifier)."""

from __future__ import annotations

from pipeline.build import build_book_script
from pipeline.classify import classify_heuristic, _coerce_book_type
from pipeline.clean import clean_paragraph
from pipeline.profiles import get_profile
from pipeline.schema import UtteranceKind

# Original verse (the same lines as pipeline/samples/poem.txt).
POEM = """The tide came in without a sound
And took the lantern from the shore
It left no footprint on the ground
And yet the light was there no more

We watched the water turn to glass
We counted every fading star
And wondered, as the slow hours pass
How far from home the drowned ones are"""

DRAMA = """HAMLET. To be, or not to be, that is the question.
OPHELIA. My lord, how does your honour for this many a day?
HAMLET. I humbly thank you; well, well, well."""

NONFICTION = """Introduction

Attention spans have declined in recent years (Smith, 2019). Others dispute this [4]. See Figure 2 for the trend. As Jones et al. note, the data is mixed.

References"""

SELFHELP = """You already have what you need. Your habits define your future, and you can change them today.

Step 1. Wake up early.
Step 2. Write down your goals.
Step 3. Review your progress every night."""

FICTION = '''"You knew the light would go out," Elara said. She did not turn from the window.

"I knew nothing of the sort," he answered.'''


# ---- classification ---------------------------------------------------------

def test_classify_detects_each_type():
    assert classify_heuristic(POEM)[0] == "poetry"
    assert classify_heuristic(DRAMA)[0] == "drama"
    assert classify_heuristic(NONFICTION)[0] == "nonfiction"
    assert classify_heuristic(SELFHELP)[0] == "selfhelp"
    assert classify_heuristic(FICTION)[0] == "general_fiction"


def test_coerce_book_type_aliases():
    assert _coerce_book_type("Science Fiction") == "fantasy_scifi"
    assert _coerce_book_type("self-help") == "selfhelp"
    assert _coerce_book_type("poem") == "poetry"
    assert _coerce_book_type("nonsense category") is None


# ---- skip-rules cleaning ----------------------------------------------------

def test_nonfiction_cleaning_strips_apparatus():
    nf = get_profile("nonfiction")
    assert clean_paragraph("The result was clear (Smith, 2019).", nf) == "The result was clear."
    assert clean_paragraph("A claim.[12] More text.", nf) == "A claim. More text."
    assert clean_paragraph("213", nf) == ""  # standalone page number dropped
    assert clean_paragraph("Figure 3. A chart of the trend.", nf) == ""  # caption dropped


def test_fiction_cleaning_is_untouched():
    fic = get_profile("general_fiction")
    assert clean_paragraph("He paused (again) before speaking.", fic) == "He paused (again) before speaking."
    assert clean_paragraph("42", fic) == "42"  # fiction keeps a lone number


# ---- verse chunking ---------------------------------------------------------

def test_poetry_builds_one_utterance_per_line(tmp_path):
    p = tmp_path / "poem.txt"
    p.write_text(POEM, encoding="utf-8")
    script = build_book_script(str(p), book_type="poetry")

    assert script.profile.chunk_mode == "verse"
    assert len(script.utterances) == 8  # 2 stanzas x 4 lines, none merged
    assert all(u.kind == UtteranceKind.verse for u in script.utterances)
    assert all(u.render.speed == 0.9 for u in script.utterances)
    # line pause within a stanza, stanza pause at the stanza end, chapter pause last.
    assert script.utterances[0].render.trail_pause_ms == 380
    assert script.utterances[3].render.trail_pause_ms == 900
    assert script.utterances[-1].render.trail_pause_ms == 2000


# ---- profile-driven build ---------------------------------------------------

def test_nonfiction_build_applies_profile_and_drops_noise(tmp_path):
    text = "Chapter One\n\n213\n\nThe economy grew (Smith, 2019). It slowed later.\n"
    p = tmp_path / "nf.txt"
    p.write_text(text, encoding="utf-8")
    script = build_book_script(str(p), book_type="nonfiction")

    assert script.meta.book_type == "nonfiction"
    assert script.cast[0].voice_id == "am_michael"  # nonfiction narrator voice
    assert all(u.render.speed == 0.97 for u in script.utterances)  # nonfiction pace
    joined = " ".join(u.text for u in script.utterances)
    assert "213" not in joined  # page number dropped
    assert "Smith" not in joined  # citation stripped
    assert "The economy grew." in joined


def test_auto_classify_sets_book_type(tmp_path):
    p = tmp_path / "poem.txt"
    p.write_text(POEM, encoding="utf-8")
    script = build_book_script(str(p))  # book_type defaults to "auto"
    assert script.meta.book_type == "poetry"
