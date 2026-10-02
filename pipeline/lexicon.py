"""Pronunciation lexicon — term -> respelling, applied to spoken text before
synthesis so any engine says a name right, not just ones with a phoneme input.

Respelling, not phonemes. A respelling ("eh-LAR-uh") is plain text that reaches
the engine exactly like the rest of the line — through the gateway's ``text``
field — so it works on Kokoro and Breeze alike. IPA or engine-specific phoneme
markup would only work on engines that parse it, which defeats the point of a
book-level, engine-agnostic lexicon.

This module only transforms text; it does not decide which lines are stale.
That's ``lexicon_terms_in`` plus the render cache key (``Renderer.cache_key``) —
running text through ``make_text_transform`` before synth means a changed
respelling changes the key, which is exactly what "stale" means here (see
STUDIO-API.md semantic #1: the cache key is the interface).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable

Lexicon = dict[str, str]

# A bracketed inline vocal event, e.g. "(sigh)" — never touched by respelling,
# same rule as Utterance.text vs. render.spoken_text: tags are for the engine's
# ears, not for word-matching.
_TAG_RE = re.compile(r"\([^)]*\)")


def load_lexicon(*paths: str) -> Lexicon:
    """Merge JSON files in order — later files win, missing files are ignored
    (so a per-book lexicon can layer on top of the global one without either
    having to exist)."""
    merged: Lexicon = {}
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            merged.update(json.load(fh))
    return merged


def save_lexicon(path: str, entries: Lexicon) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2, ensure_ascii=False, sort_keys=True)


def _compile(lexicon: Lexicon) -> tuple[re.Pattern[str], dict[str, tuple[str, str]]] | tuple[None, None]:
    if not lexicon:
        return None, None
    # Longest key first: with a single alternation, the regex engine tries
    # alternatives left-to-right at each position, so ordering longest-first is
    # what makes a longer phrase win over a shorter one it contains.
    keys = sorted(lexicon, key=len, reverse=True)
    alternation = "|".join(re.escape(k) for k in keys)
    # \b on both ends is what keeps "Elaraville" untouched — there's no word
    # boundary between "Elara" and the "ville" that follows it, so the pattern
    # simply doesn't match there. The optional possessive is captured
    # separately so it survives the substitution unmodified.
    # Lookarounds, not \b: a key that ends in punctuation ("Dr.", "C++") has a
    # non-word character on both sides of its edge, so \b could never match it,
    # and exactly the abbreviations a lexicon exists for were silently skipped.
    pattern = re.compile(rf"(?<!\w)(?P<term>{alternation})(?P<poss>'s)?(?!\w)", re.IGNORECASE)
    # Case-insensitive lookup back to the canonical (key, respelling) pair.
    lower_map = {k.lower(): (k, v) for k, v in lexicon.items()}
    return pattern, lower_map


def _respell(match: re.Match[str], lower_map: dict[str, tuple[str, str]]) -> str:
    term = match.group("term")
    poss = match.group("poss") or ""
    key, respelling = lower_map[term.lower()]
    # A key authored capitalized (a proper noun — "Elara") means the author's
    # respelling case is deliberate and used verbatim, however the name is
    # cased at this occurrence. A key authored lowercase (an ordinary word that
    # only reads capitalized because it opened a sentence) gets its respelling
    # capitalized to match, so "Quinoa is ..." still reads as a real sentence.
    if term[:1].isupper() and key[:1].islower() and respelling[:1].islower():
        respelling = respelling[0].upper() + respelling[1:]
    return respelling + poss


def apply_lexicon(text: str, lexicon: Lexicon) -> str:
    """Replace whole-word/phrase matches with their respelling: case-insensitive,
    longest key first, never inside a bracketed tag, never inside another word.
    Possessives keep the ``'s``: "Elara's" -> "eh-LAR-uh's"."""
    pattern, lower_map = _compile(lexicon)
    if pattern is None:
        return text

    out: list[str] = []
    pos = 0
    for m in _TAG_RE.finditer(text):
        out.append(pattern.sub(lambda mm: _respell(mm, lower_map), text[pos : m.start()]))
        out.append(m.group(0))  # the tag itself, untouched
        pos = m.end()
    out.append(pattern.sub(lambda mm: _respell(mm, lower_map), text[pos:]))
    return "".join(out)


def lexicon_terms_in(text: str, lexicon: Lexicon) -> list[str]:
    """Which lexicon keys occur in ``text`` (first-occurrence order, no dupes).
    Used by the studio to flip a line stale when the entry it depends on
    changes, even before the render cache key itself has moved."""
    pattern, lower_map = _compile(lexicon)
    if pattern is None:
        return []
    found: list[str] = []
    for m in pattern.finditer(text):
        key = lower_map[m.group("term").lower()][0]
        if key not in found:
            found.append(key)
    return found


def make_text_transform(lexicon: Lexicon) -> Callable[[str], str]:
    """``Renderer(text_transform=...)`` factory: bind a lexicon once, get back
    the plain ``str -> str`` callable the renderer applies before the cache key
    is computed and before synth."""

    def transform(text: str) -> str:
        return apply_lexicon(text, lexicon)

    return transform
