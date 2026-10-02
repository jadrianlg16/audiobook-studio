"""Sentence chunking + the pause budget.

Two hard rules from the design: never split mid-sentence, and never let the
splitter fire on an abbreviation ("Dr.") or a decimal ("3.14"). This is a
dependency-light, abbreviation-guarded splitter — good enough for narration and
trivially swappable for a heavier segmenter later.

Pauses are expressed as trailing silence (ms) per utterance; the renderer
inserts that silence between clips so prosody doesn't run together.
"""

from __future__ import annotations

import re

# Pause budget (ms). Tuneable — these shape how the finished audiobook "breathes".
SENTENCE_PAUSE_MS = 200
PARAGRAPH_PAUSE_MS = 600
SCENE_PAUSE_MS = 1200
CHAPTER_PAUSE_MS = 2000

# Lowercased, trailing dot stripped. Words that end in "." but don't end a sentence.
# The second block is the publishing set — the ones that appear on every page of
# a real book ("see Ch. 4", "Mr. Vane", "vol. II, pp. 20-24") and that used to
# break a sentence in half mid-parenthesis.
_ABBREV = {
    "mr", "mrs", "ms", "dr", "st", "sr", "jr", "vs", "prof", "gen", "rev", "hon",
    "capt", "sgt", "lt", "col", "gov", "sen", "rep", "co", "inc", "ltd", "no",
    "vol", "fig", "al", "etc", "e.g", "i.e", "viz", "cf", "mt", "ft", "dept",
    "univ", "assn", "bros", "phd", "md", "esq", "messrs", "mme", "mlle",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    # publishing apparatus + honorifics that survived into running prose
    "ch", "chap", "chaps", "pp", "vols", "figs", "ed", "eds", "edn", "trans",
    "illus", "rpt", "ser", "supp", "sec", "sects", "art", "approx", "ca",
    "circa", "ibid", "op", "cit", "et", "esp", "incl", "min", "max", "misc",
    "adm", "maj", "cpl", "pvt", "insp", "supt", "bp", "abp",
}

# A sentence boundary: terminal punctuation, optionally a closing quote/bracket,
# optionally a footnote marker ("...missing.[1] The ledger..."), then whitespace.
# The marker has to be *part of the boundary* or the sentence after it never
# starts: the splitter sees "." immediately followed by "[" and reads on.
_FOOTNOTE_MARK = r"(?:\[\d{1,3}\]|\(\d{1,3}\)|[¹²³⁰-⁹]+)"
_BOUNDARY = re.compile(rf"[.!?]['\"’”)\]]?{_FOOTNOTE_MARK}?['\"’”]?\s+")
_LAST_TOKEN = re.compile(r"([A-Za-z][A-Za-z.]*)['\"’”)\]]?\.?\s*$")

# "1.", "a)", "(3)", "iv." standing alone — an enumerator, not a sentence. Read
# as its own utterance the narrator says "One." and then pauses, which is how a
# footnote body or a numbered list gets mangled.
_ENUMERATOR = re.compile(
    r"^\(?(?:\d{1,3}|[a-zA-Z]|[ivxlcdmIVXLCDM]{1,7})[.)]$"
)


def _ends_with_abbrev(segment: str) -> bool:
    m = _LAST_TOKEN.search(segment)
    if not m:
        return False
    word = m.group(1).rstrip(".").lower()
    if word in _ABBREV:
        return True
    return len(word) == 1  # single-letter initials, e.g. "H. G. Wells"


def _is_bare_enumerator(segment: str) -> bool:
    return bool(_ENUMERATOR.match(segment.strip()))


def _unclosed_at(text: str, index: int, open_ch: str, close_ch: str) -> bool:
    """Is ``index`` inside an open ``open_ch`` … ``close_ch`` pair that closes later?

    "closes later" is the guard that keeps this safe on real prose: a paragraph
    of dialogue that opens a quote and never closes it (the convention for a
    speech that runs on into the next paragraph) would otherwise become one
    unsplittable block. If the bracket never closes, we let the splitter work.
    """
    depth = text.count(open_ch, 0, index) - text.count(close_ch, 0, index)
    return depth > 0 and close_ch in text[index:]


def _inside_quotes(text: str, index: int) -> bool:
    """Straight quotes are counted by parity, curly ones as a real pair."""
    if text.count('"', 0, index) % 2 == 1 and '"' in text[index:]:
        return True
    return _unclosed_at(text, index, "“", "”")


def split_lines(text: str) -> list[str]:
    """Verse mode: one line per utterance. Line breaks are semantic in poetry,
    so we keep them instead of merging into sentences."""
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def split_sentences(text: str) -> list[str]:
    """Split a paragraph into sentences, guarding abbreviations and decimals.

    Five things stop a boundary from firing, all of them observed breaking real
    prose: an abbreviation ("see Ch. 4"), a decimal, a bare enumerator ("1." at
    the head of a footnote body), a position inside an unclosed parenthesis, and
    a position inside an unclosed quotation. A footnote marker sitting on the
    period ("...missing.[1] The ledger...") is swallowed *by* the boundary
    instead of hiding it.
    """
    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        return []

    sentences: list[str] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        if m.start() < start:  # inside a piece we already emitted
            continue
        nxt = text[m.end()] if m.end() < len(text) else ""
        # Only break if what follows looks like a new sentence (capital/quote/digit).
        if nxt and not (nxt.isupper() or nxt.isdigit() or nxt in "\"'“([‘"):
            continue
        segment = text[start : m.start() + 1]
        if _ends_with_abbrev(segment):
            continue
        if _is_bare_enumerator(text[start : m.end()]):
            continue  # merge the enumerator into the text it introduces
        if _unclosed_at(text, m.end(), "(", ")") or _inside_quotes(text, m.end()):
            continue
        sentences.append(text[start : m.end()].strip())
        start = m.end()

    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences
