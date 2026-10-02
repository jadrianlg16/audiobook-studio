"""Classify a book into one of ``BOOK_TYPES`` so the right production profile is
chosen. Two paths:

- ``classify_heuristic`` — signal-based, offline, deterministic. Always available.
- ``classify_llm`` — asks Ollama/Gemini for a book-type label (urllib, no SDK).
  Optional; any failure/garbage falls back to the heuristic.

The heuristic looks for structural fingerprints: short punctuation-light lines
(verse), ALL-CAPS speaker cues (drama), citations/footnotes/section words
(nonfiction), second-person + numbered steps (self-help), dialogue (fiction),
genre keywords (fantasy/sci-fi).
"""

from __future__ import annotations

import json
import re
import urllib.request

from .profiles import BOOK_TYPES

_CITATION = re.compile(r"\([A-Z][A-Za-z.'’-]+(?:\s+et al\.?)?(?:\s+(?:and|&)\s+[A-Z][A-Za-z.'’-]+)?,?\s+\d{4}[a-z]?\)")
_FOOTNOTE = re.compile(r"\[\d{1,3}\]")
_SECTION = re.compile(r"\b(references|bibliography|introduction|conclusion|abstract|works cited|further reading|appendix|methodology)\b", re.I)
_FIGTAB = re.compile(r"\b(figure|table|fig\.)\s+\d+\b", re.I)
_DRAMA_CUE = re.compile(r"^\s*[A-Z][A-Z .'’-]{1,28}[.:]\s+\S")
_SELFHELP_KW = re.compile(r"\b(habit|habits|goal|goals|success|mindset|achieve|improve|productiv\w+|motivat\w+|routine|potential)\b", re.I)
_FANTASY_KW = re.compile(r"\b(magic|wizard|dragon|sword|kingdom|realm|spell|sorcer\w+|elf|elves|throne|prophecy|starship|galaxy|android|hyperspace)\b", re.I)
_STEP = re.compile(r"^\s*(?:step\s+\d+|\d+[.)])\s", re.I | re.M)
_YOU = re.compile(r"\byou(?:r|rs)?\b", re.I)


def classify_heuristic(text: str) -> tuple[str, dict[str, float]]:
    """Return ``(book_type, scores)``. Conservative: defaults to general fiction
    unless a fingerprint is clear."""
    sample = text[:8000]
    words = re.findall(r"[A-Za-z']+", sample)
    nwords = max(1, len(words))
    lines = text.splitlines()
    nonblank = [ln.strip() for ln in lines if ln.strip()]
    nlines = max(1, len(nonblank))

    scores: dict[str, float] = {}

    # Verse: many short lines that don't end in terminal punctuation.
    short_noterm = sum(1 for ln in nonblank if len(ln) < 55 and ln[-1] not in ".!?:;\"'”’)")
    avg_len = sum(len(ln) for ln in nonblank) / nlines
    verse = len(nonblank) >= 4 and short_noterm / nlines >= 0.5 and avg_len < 55
    scores["poetry"] = 3.0 if verse else 0.0

    # Drama: speaker cues like "HAMLET. …".
    cues = sum(1 for ln in lines if _DRAMA_CUE.match(ln))
    scores["drama"] = 3.0 if cues >= 3 and cues / nlines >= 0.4 else min(2.0, cues * 0.5)

    # Nonfiction: scholarly apparatus.
    nf = (
        len(_CITATION.findall(sample)) * 2
        + len(_FOOTNOTE.findall(sample))
        + len(_SECTION.findall(sample))
        + len(_FIGTAB.findall(sample))
        + sample.lower().count("et al") * 2
    )
    scores["nonfiction"] = min(4.0, nf * 0.7)

    # Self-help: heavy second person + numbered steps.
    you = len(_YOU.findall(sample))
    steps = len(_STEP.findall(text))
    sh = (you / nwords) * 100 * 0.4 + steps * 0.9 + len(_SELFHELP_KW.findall(sample)) * 0.4
    scores["selfhelp"] = min(4.0, sh) if you >= 3 else 0.0

    # Fantasy / sci-fi: genre vocabulary (on top of being fiction).
    scores["fantasy_scifi"] = min(3.0, len(_FANTASY_KW.findall(sample)) * 0.8)

    # Fiction baseline: dialogue density + a small floor so narrative wins ties.
    dq = sample.count('"') + sample.count("“")
    scores["general_fiction"] = min(2.5, dq / 10) + 0.5

    best = max(scores, key=lambda k: scores[k])
    if scores[best] < 0.8:
        best = "general_fiction"
    return best, scores


def _coerce_book_type(value: object) -> str | None:
    if not value:
        return None
    v = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if v in BOOK_TYPES:
        return v
    aliases = {
        "fiction": "general_fiction", "novel": "general_fiction",
        "sci_fi": "fantasy_scifi", "science_fiction": "fantasy_scifi",
        "fantasy": "fantasy_scifi", "verse": "poetry", "poem": "poetry",
        "play": "drama", "self_help": "selfhelp", "how_to": "selfhelp",
        "reference": "technical", "textbook": "technical",
        "biography": "nonfiction", "history": "nonfiction", "classic": "literary",
    }
    return aliases.get(v)


_PROMPT = (
    "Classify the book excerpt below into exactly one category from this list: {types}. "
    'Reply ONLY with JSON: {{"book_type": "<one category>"}}.\n\nEXCERPT:\n"""\n{sample}\n"""'
)


def _post_json(url: str, payload: dict, timeout: float = 60.0) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_book_type(raw: str) -> str | None:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return _coerce_book_type(json.loads(m.group(0)).get("book_type"))
    except json.JSONDecodeError:
        return None


def classify_llm(
    text: str, *, provider: str = "ollama", model: str = "llama3.2",
    ollama_url: str = "http://host.docker.internal:11434", gemini_key: str = "",
) -> str | None:
    """Best-effort LLM classification. Returns a valid book_type or None."""
    prompt = _PROMPT.format(types=", ".join(BOOK_TYPES), sample=text[:6000])
    try:
        if provider == "ollama":
            data = _post_json(
                f"{ollama_url.rstrip('/')}/api/generate",
                {"model": model, "prompt": prompt, "stream": False, "format": "json"},
            )
            return _extract_book_type(data.get("response", ""))
        if provider == "gemini":
            if not gemini_key:
                return None
            data = _post_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}",
                {"contents": [{"parts": [{"text": prompt}]}],
                 "generationConfig": {"responseMimeType": "application/json"}},
            )
            return _extract_book_type(data["candidates"][0]["content"]["parts"][0]["text"])
    except Exception:  # noqa: BLE001 - any failure → caller falls back to heuristic
        return None
    return None


def classify_book(text: str, *, use_llm: bool = False, **llm_kwargs) -> str:
    """Pick a book_type. Tries the LLM first when asked, else/otherwise the
    heuristic. Never raises — always returns a valid book_type."""
    if use_llm:
        bt = classify_llm(text, **llm_kwargs)
        if bt:
            return bt
    return classify_heuristic(text)[0]
