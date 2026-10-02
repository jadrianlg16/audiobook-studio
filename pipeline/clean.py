"""Content cleaning driven by a profile's skip-rules. This is the correctness
win for nonfiction: strip the apparatus a narrator should never read aloud —
page numbers, footnote/endnote markers, inline citations, figure/table captions.

``clean_paragraph`` returns the cleaned text, or ``""`` when the whole block
should be dropped (a standalone page number or figure caption). Rules only fire
when the active profile enables them, so fiction is left untouched — with one
exception: a footnote marker glued to a word or to terminal punctuation
(``lie.[1]``, ``Somme¹``) is stripped under **every** profile, because it is
apparatus in a novel exactly as much as it is in a textbook and no narrator has
ever read one aloud.
"""

from __future__ import annotations

import re

from .profiles import ProductionProfile

# Inline footnote/endnote markers: "[12]" and Unicode superscripts (¹²³ …).
_FOOTNOTE = re.compile(r"\[\d{1,3}\]|[²³¹⁰-⁹]+")
# The same marker where it is *attached* to a word or to terminal punctuation —
# "…reasons to lie.[1] The ledger…", "…the Somme¹". A marker is never speech, in
# any genre, so this one is not profile-gated: a novel with endnotes used to send
# "[1]" to the engine verbatim because only the nonfiction profiles stripped it.
_ATTACHED_FOOTNOTE = re.compile(r"(?<=[\w.,!?;:)\"'”’])(?:\[\d{1,3}\]|[²³¹⁰-⁹]+)")
# Inline citation: "(Smith, 2019)", "(Jones et al. 2020)", "(Doe and Roe, 1999)".
_CITATION = re.compile(
    r"\((?:see\s+)?[A-Z][A-Za-z.'’-]+(?:\s+et al\.?)?(?:\s+(?:and|&)\s+[A-Z][A-Za-z.'’-]+)?,?\s+\d{4}[a-z]?(?:,\s*pp?\.?\s*\d+(?:[-–]\d+)?)?\)"
)
# A block that is *only* a page number: "213", "Page 12", "— 12 —".
_PAGE_ONLY = re.compile(r"^[\s\-–—•|]*(?:page\s+)?\d{1,4}[\s\-–—•|]*$", re.I)
# A figure/table caption block: "Figure 3. The …", "Table 2: …".
_FIG_CAP = re.compile(r"^\s*(?:figure|fig\.|table|plate|chart|exhibit|scheme)\s+\d+[.:]?\b", re.I)

_MULTISPACE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?])")


def clean_paragraph(text: str, profile: ProductionProfile) -> str:
    """Apply the profile's skip-rules. Returns cleaned text, or "" to drop."""
    stripped = text.strip()
    if not stripped:
        return ""

    # Whole-block drops.
    if profile.strip_page_numbers and _PAGE_ONLY.match(stripped):
        return ""
    if profile.strip_figure_captions and _FIG_CAP.match(stripped) and len(stripped) < 160:
        return ""

    out = _ATTACHED_FOOTNOTE.sub("", text)
    if profile.strip_citations:
        out = _CITATION.sub("", out)
    if profile.strip_footnote_markers:
        out = _FOOTNOTE.sub("", out)

    # Tidy whitespace left behind by removals.
    out = _SPACE_BEFORE_PUNCT.sub(r"\1", out)
    out = _MULTISPACE.sub(" ", out).strip()
    return out
