"""Speaker attribution: split prose into narration vs. dialogue and decide *who*
speaks each quote. Heuristic (deterministic, offline) — good coverage of the
common patterns, which is most fiction:

  "..." said Elara      Elara said, "..."      "..." she called (→ pronoun)
  "..." asked Marcus     Marcus: "..."          continuation within a paragraph
                                                alternation across paragraphs

It won't nail every line (full coreference is a research problem), so it says so:
every quote carries the **basis** it was decided on and a **confidence**, and
anything below ``ABSTAIN_BELOW`` becomes ``"Unknown"`` (which still gets its own
voice). That is the whole point — a wrong speaker is invisible in the finished
audio, an ``Unknown`` is a work item somebody can see and fix in twenty minutes.
Cross-paragraph alternation, the weakest guess of the four, is deliberately
scored under the bar: it used to be stated as confidently as an explicit tag.

An LLM pass can be layered on later for accuracy — the ``intent``/cast structure
already supports it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_VERBS = (
    r"(?:said|says|asked|asks|replied|replies|answered|answers|whispered|whispers|"
    r"shouted|shouts|cried|cries|muttered|mutters|murmured|murmurs|called|calls|"
    r"continued|continues|added|adds|began|begins|exclaimed|exclaims|snapped|snaps|"
    r"breathed|breathes|sighed|sighs|growled|growls|hissed|hisses|laughed|laughs|"
    r"roared|demanded|demands|insisted|insists|observed|remarked|noted|agreed|"
    r"admitted|admits|warned|warns|offered|offers|wondered|wonders|repeated|repeats|"
    r"declared|declares|responded|responds|gasped|stammered|blurted|retorted|"
    r"countered|mused|chuckled|barked|pleaded|urged|scoffed|sneered|yelled|"
    r"whimpered|prompted|concluded|finished|put in|went on)"
)
_TITLE = r"(?:Mr|Mrs|Ms|Miss|Dr|Prof|Lord|Lady|Sir|Madam|Captain|Colonel|Sergeant|King|Queen|Prince|Princess|Aunt|Uncle)\.?"
_NAME = rf"(?:{_TITLE}\s+)?[A-Z][a-z]+(?:\s[A-Z][a-z]+)?"

_LEAD = r"[\s,.\-—–\"“”'’]*"
AFTER_NAME_VERB = re.compile(rf"^{_LEAD}(?P<name>{_NAME})\s+{_VERBS}\b")
AFTER_VERB_NAME = re.compile(rf"^{_LEAD}{_VERBS}\s+(?P<name>{_NAME})\b")
AFTER_PRON_VERB = re.compile(rf"^{_LEAD}(?P<pron>[Hh]e|[Ss]he|[Tt]hey)\s+{_VERBS}\b")
AFTER_VERB_PRON = re.compile(rf"^{_LEAD}{_VERBS}\s+(?P<pron>him|her|them|he|she|they)\b")
BEFORE_NAME_VERB = re.compile(rf"(?P<name>{_NAME})\s+{_VERBS}\b[^\"“”.!?]*$")
BEFORE_COLON = re.compile(rf"(?P<name>{_NAME}):\s*$")
BEFORE_PRON_VERB = re.compile(rf"(?P<pron>He|She|They)\s+{_VERBS}\b[^\"“”.!?]*$")

_QUOTE = re.compile(r"\"[^\"]*\"|“[^”]*”")
_PROPER = re.compile(r"[A-Z][a-z]+")
_PRON = re.compile(r"\b(he|him|his|she|her|they|them)\b", re.I)

_TITLE_GENDER = {
    "mr": "m", "sir": "m", "lord": "m", "king": "m", "prince": "m", "uncle": "m",
    "mrs": "f", "ms": "f", "miss": "f", "lady": "f", "madam": "f", "queen": "f",
    "princess": "f", "aunt": "f",
}
_STOP = {
    "the", "a", "an", "and", "but", "or", "so", "then", "there", "here", "when",
    "where", "why", "how", "he", "she", "they", "it", "his", "her", "we", "you",
    "i", "at", "in", "on", "by", "as", "for", "no", "yes", "of", "to", "with",
    "that", "this", "if", "not", "now", "up", "out", "well", "oh",
}


# How much each route to a speaker is worth. Only the explicit tag is evidence;
# the rest are inferences of decreasing strength, and alternation sits under the
# abstain bar on purpose (it is a coin flip dressed as an answer).
CONFIDENCE = {
    "tag": 0.95,
    "continuation": 0.7,
    "pronoun": 0.6,
    "pronoun_guess": 0.4,  # a pronoun with two unknown-gender names in play
    "alternation": 0.4,
    "none": 0.0,
}
ABSTAIN_BELOW = 0.5
"""Under this, the speaker becomes ``"Unknown"`` — the basis/confidence are kept
so a review pass can sort by them."""


@dataclass
class Turn:
    speaker: str
    kind: str  # "narration" | "dialogue"
    text: str
    basis: str = "none"
    confidence: float = 1.0


def split_quotes(paragraph: str) -> list[tuple[str, bool]]:
    """Return alternating (text, is_quote) spans (quotes keep their marks)."""
    spans: list[tuple[str, bool]] = []
    pos = 0
    for m in _QUOTE.finditer(paragraph):
        if m.start() > pos:
            spans.append((paragraph[pos : m.start()], False))
        spans.append((m.group(0), True))
        pos = m.end()
    if pos < len(paragraph):
        spans.append((paragraph[pos:], False))
    return spans


# A dialogue tag: the little clause that carries a quote — "she said",
# "Marcus answered", "said the colonel". Six words is the ceiling; past that it
# is narration doing its own work ("she said, and no one answered her" is two
# units, and only the first is a tag).
_TAG_LEAD = r"^[\s,.\-—–\"“”'’]*"
_TAG_NAME_VERB = re.compile(rf"{_TAG_LEAD}(?:{_NAME}|He|She|They|I|We)\s+{_VERBS}\b")
_MAX_TAG_WORDS = 6


def is_dialogue_tag(text: str) -> bool:
    """Is ``text`` a dialogue tag (and so part of the quote's breath group)?

    ``"You let it go out," she said.`` is **one** breath group: a narrator reads
    straight through the comma. Splitting it into two utterances with a full
    sentence pause between them puts a 350 ms hole in the middle of every tagged
    line in the book. ``build.py`` uses this to close that gap.

    Deliberately narrow: a lowercase start (``she said.``, ``said the colonel.``)
    or a name/pronoun followed by a speech verb, and short either way.
    """
    stripped = text.strip().lstrip("\"“”'’—–- ")
    if not stripped:
        return False
    if len(stripped.split()) > _MAX_TAG_WORDS:
        return False
    if stripped[0].islower():
        return True
    return bool(_TAG_NAME_VERB.match(stripped))


def _clean_name(raw: str) -> tuple[str | None, str]:
    raw = raw.strip()
    gender = "n"
    m = re.match(rf"^({_TITLE})\s+(.*)$", raw)
    if m:
        gender = _TITLE_GENDER.get(m.group(1).rstrip(".").lower(), "n")
        raw = m.group(2).strip()
    if not raw or raw.split()[0].lower() in _STOP:
        return None, "n"
    return raw, gender


class DialogueContext:
    def __init__(self) -> None:
        self.characters: dict[str, str] = {}  # name -> 'f' | 'm' | 'n'
        self.last_male: str | None = None
        self.last_female: str | None = None
        self.last_name: str | None = None
        self.recent: list[str] = []  # recent distinct speakers, most recent last
        self.mentioned: list[str] = []  # recent distinct names, spoken or narrated

    def register(self, name: str, gender: str = "n") -> None:
        cur = self.characters.get(name, "n")
        self.characters[name] = gender if gender != "n" else cur
        if self.characters[name] == "m":
            self.last_male = name
        elif self.characters[name] == "f":
            self.last_female = name
        self.last_name = name

    def note_speaker(self, name: str) -> None:
        if not name or name == "Unknown":
            return
        if not self.recent or self.recent[-1] != name:
            self.recent.append(name)
            self.recent = self.recent[-4:]
        self.register(name, self.characters.get(name, "n"))
        self.last_name = name
        self.note_mention(name)

    def note_mention(self, name: str) -> None:
        if not name or name == "Unknown":
            return
        if name in self.mentioned:
            self.mentioned.remove(name)
        self.mentioned.append(name)
        self.mentioned = self.mentioned[-4:]

    def note_narration(self, text: str) -> None:
        cur = self.last_name  # carry the antecedent across the sentence boundary
        named_here = False  # a name in *this* text is the antecedent by construction
        for m in re.finditer(r"[A-Z][a-z]+|\b(?:he|him|his|she|her|they|them)\b", text):
            tok = m.group(0)
            low = tok.lower()
            # A bare pronoun inherited from an earlier sentence, with two
            # unknown-gender names in play, is the guess that taught the context
            # "Tomas is female" from Mara's "she said." Don't learn from it.
            guessing = not named_here and self._ambiguous()
            if low in ("he", "him", "his"):
                # Never flip a known-female antecedent; only learn/refresh male.
                if cur and self.characters.get(cur, "n") != "f":
                    if self.characters.get(cur, "n") == "n" and not guessing:
                        self.characters[cur] = "m"
                    if self.characters.get(cur, "n") == "m":
                        self.last_male = cur
            elif low in ("she", "her"):
                if cur and self.characters.get(cur, "n") != "m":
                    if self.characters.get(cur, "n") == "n" and not guessing:
                        self.characters[cur] = "f"
                    if self.characters.get(cur, "n") == "f":
                        self.last_female = cur
            elif low in ("they", "them"):
                continue
            elif low not in _STOP:
                cur = tok
                named_here = True
                self.last_name = tok
                self.note_mention(tok)

    def _ambiguous(self) -> bool:
        """Two or more recently mentioned names of unknown gender: adopting one
        of them for a pronoun is a coin flip, not a resolution."""
        unknown = {n for n in self.mentioned if self.characters.get(n, "n") == "n"}
        return len(unknown) >= 2

    def resolve(self, pron: str, before: str = "") -> tuple[str | None, bool]:
        """Return ``(name, strong)``. ``strong`` is False when the name was merely
        the most recent one around and its gender had to be assumed while another
        unknown-gender name was also in play — the case that voiced Mara's line as
        Tomas. The exception is an action beat: a name that appears in ``before``
        (the narration right in front of the quote, same paragraph — ``Marcus did
        not look up. "…" he answered.``) is the antecedent by construction. A weak
        guess is never registered as that character's gender."""
        p = pron.lower()

        def adopt(gender: str) -> tuple[str | None, bool]:
            name = self.last_name
            if not name:
                return None, False
            other = "f" if gender == "m" else "m"
            if self.characters.get(name, "n") == other:
                return None, False
            if self._ambiguous() and not re.search(rf"\b{re.escape(name)}\b", before):
                return name, False
            self.register(name, gender)
            return name, True

        if p in ("he", "him", "his"):
            return (self.last_male, True) if self.last_male else adopt("m")
        if p in ("she", "her"):
            return (self.last_female, True) if self.last_female else adopt("f")
        name = self.last_name  # they/them → most recent name
        strong = not self._ambiguous() or bool(name and re.search(rf"\b{re.escape(name)}\b", before))
        return name, strong

    def alternate(self, prev: str | None) -> str | None:
        for name in reversed(self.recent):
            if name != prev:
                return name
        return None


class Attributer:
    def __init__(self) -> None:
        self.ctx = DialogueContext()
        self._last_speaker: str | None = None

    def _register(self, raw: str) -> str | None:
        name, gender = _clean_name(raw)
        if name is None:
            return None
        self.ctx.register(name, gender)
        return name

    def reset(self) -> None:
        """Forget who was speaking. Called at every **chapter boundary**.

        Without this the first quote of a chapter inherits the last speaker of
        the previous one (via continuation/alternation/pronoun recency) — a
        character carried across a scene break they are not in, voiced wrong for
        the whole opening. Learned *gender* is kept (``ctx.characters``): it is a
        property of the character, not of where we are in the book, and casting
        reads it after the whole script is built.
        """
        self.ctx.last_male = None
        self.ctx.last_female = None
        self.ctx.last_name = None
        self.ctx.recent = []
        self._last_speaker = None

    def _attribute_quote(
        self, before: str, after: str, para_last: str | None
    ) -> tuple[str, str]:
        """Return ``(speaker, basis)``. ``("Unknown", "none")`` when nothing fits."""
        for pat in (AFTER_NAME_VERB, AFTER_VERB_NAME):
            m = pat.match(after)
            if m:
                name = self._register(m.group("name"))
                if name:
                    return name, "tag"
        m = BEFORE_NAME_VERB.search(before) or BEFORE_COLON.search(before)
        if m:
            name = self._register(m.group("name"))
            if name:
                return name, "tag"
        for pat in (AFTER_PRON_VERB, AFTER_VERB_PRON):
            m = pat.match(after)
            if m:
                r, strong = self.ctx.resolve(m.group("pron"), before)
                if r:
                    return r, "pronoun" if strong else "pronoun_guess"
        m = BEFORE_PRON_VERB.search(before)
        if m:
            r, strong = self.ctx.resolve(m.group("pron"), before)
            if r:
                return r, "pronoun" if strong else "pronoun_guess"
        if para_last and para_last != "Unknown":  # same speaker continuing
            return para_last, "continuation"
        alt = self.ctx.alternate(self._last_speaker)  # cross-paragraph back-and-forth
        if alt:
            return alt, "alternation"
        return "Unknown", "none"

    def attribute(self, paragraph: str) -> list[Turn]:
        turns: list[Turn] = []
        spans = split_quotes(paragraph)
        para_last: str | None = None
        for idx, (text, is_quote) in enumerate(spans):
            if not is_quote:
                if text.strip():
                    self.ctx.note_narration(text)
                    turns.append(Turn("Narrator", "narration", text.strip()))
                continue
            before = spans[idx - 1][0] if idx > 0 and not spans[idx - 1][1] else ""
            after = spans[idx + 1][0] if idx + 1 < len(spans) and not spans[idx + 1][1] else ""
            guess, basis = self._attribute_quote(before, after, para_last)
            confidence = CONFIDENCE.get(basis, 0.0)
            if basis == "pronoun_guess":  # the schema's basis enum has no guess value
                basis = "pronoun"
            # Abstain rather than assert. The basis and the number survive so the
            # studio can walk the low-confidence lines before a single render.
            speaker = guess if confidence >= ABSTAIN_BELOW else "Unknown"
            self.ctx.note_speaker(speaker)
            self._last_speaker = speaker
            para_last = speaker
            turns.append(Turn(speaker, "dialogue", text.strip(), basis, confidence))
        return turns
