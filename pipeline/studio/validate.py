"""Every check that has to happen *before* something is written to disk.

The rule this module exists to enforce: **an edit the studio cannot round-trip
must never reach ``script.json``**. The bug this prevents —
``PATCH {"speed": 99}`` was applied by plain attribute assignment (pydantic does
not re-validate on assignment), written out, and then failed validation on the
*next* load, so every later ``GET`` on that book was a 500 with no way back
through the API. One bad number bricked a book.

Two consequences run through this file:

- **Validate by constructing the real model, not by re-listing its bounds.**
  ``validated_render`` builds a fresh ``Render`` from the merged values and lets
  ``schema.Render``'s own constraints answer. A bound added to the schema
  tomorrow is enforced here today, and there is no second copy of "0.1 < speed
  <= 4.0" to drift.
- **Cross-field rules need the catalog.** An engine id means nothing on its own;
  ``voice_id`` is only meaningful against the engine's voice list, and
  ``spoken_text`` is only meaningful against the engine's tag vocabulary and the
  line's own text. Those checks take the merged engine catalog
  (``engines.py``) so they work with the gateway down.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from pydantic import ValidationError

from ..schema import Render
from .models import EngineOption

try:  # the gateway's normalization rule, copied into the pipeline by direct.py
    from ..direct import _squash as squash
except ImportError:  # pragma: no cover - only if direct.py is mid-rewrite
    def squash(text: str) -> str:
        collapsed = " ".join(text.split())
        return re.sub(r"\s+([,.;:!?…)\]}»”’\"'])", r"\1", collapsed)


class Invalid(ValueError):
    """A client-side mistake with a message a person can act on → HTTP 422."""


# ---- pydantic errors, flattened ---------------------------------------------

def flatten(exc: ValidationError) -> str:
    """Pydantic's error list as one human sentence.

    The contract says errors are ``{"detail": "<human message>"}``; FastAPI's
    default 422 body puts a list of dicts there instead, which every client then
    has to special-case. One line per problem, field first, is both.
    """
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(x) for x in error.get("loc", ()) if x not in ("body", "query"))
        message = error.get("msg", "is invalid")
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts) or "the request body is not valid"


# ---- engines, voices --------------------------------------------------------

def engine_option(engine_id: str, catalog: Iterable[EngineOption]) -> EngineOption:
    """The catalog entry for ``engine_id``, or ``Invalid`` naming the real ones."""
    options = list(catalog)
    for option in options:
        if option.id == engine_id:
            return option
    known = ", ".join(sorted(o.id for o in options)) or "(none)"
    raise Invalid(f"unknown engine {engine_id!r} — this studio knows: {known}")


def check_voice(option: EngineOption, voice_id: str | None) -> None:
    """A voice must belong to the engine that is going to speak it.

    An engine with no named voices (Breeze designs one from prose) takes
    ``null``: a voice id it never published is a value the synth request drops
    on the floor, which reads to the user as "the voice I picked did nothing".
    """
    if voice_id is None:
        return
    ids = [v.get("id") for v in option.voices]
    if not ids:
        raise Invalid(
            f"engine {option.id!r} has no named voices (it designs a voice from a "
            f"description) — leave voice_id empty and set voice_design on the cast entry"
        )
    if voice_id not in ids:
        raise Invalid(
            f"engine {option.id!r} has no voice {voice_id!r} — its voices are: "
            f"{', '.join(str(v) for v in ids)}"
        )


# ---- spoken_text ------------------------------------------------------------

# Anything that *looks* like an inline event: "(sigh)", "<laugh>", "[pause]".
# Used only to write a better error message — the fidelity check below is what
# actually decides, so an author's genuine parenthetical is never a tag.
_TAGGISH = re.compile(r"[(<\[][^()<>\[\]\n]{0,40}[)>\]]")


def strip_tags(text: str, tags: Iterable[str]) -> str:
    ordered = sorted({t for t in tags if t}, key=len, reverse=True)
    if not ordered:
        return text
    pattern = re.compile("|".join(re.escape(t) for t in ordered))
    return pattern.sub(" ", text)


def check_spoken_text(spoken: str, text: str, option: EngineOption) -> None:
    """Contract rule 2, enforced at the point of entry.

    ``spoken_text`` is what the engine hears; ``text`` is what the reader sees.
    The director's output is already held to this standard gateway-side (strip
    the tags and what remains must be the source passage) — before this, a
    manual PATCH could store anything at all, and the engine would read a
    sentence the book does not contain.
    """
    allowed = list(option.audio_tags)
    unknown = [t for t in _TAGGISH.findall(spoken) if t not in allowed and t not in text]
    if unknown:
        if allowed:
            raise Invalid(
                f"engine {option.id!r} does not understand the tag {unknown[0]!r} — "
                f"it accepts: {', '.join(allowed)}"
            )
        raise Invalid(
            f"engine {option.id!r} has no inline tag vocabulary, so {unknown[0]!r} would be "
            f"read out as words — clear spoken_text or route the line to a tag-capable engine"
        )
    if squash(strip_tags(spoken, allowed)) != squash(text):
        raise Invalid(
            "spoken_text must be this line's text with only inline tags added — "
            "stripping the tags from it does not reproduce the line"
        )


# ---- the render block -------------------------------------------------------

def validated_render(render: Render, changes: Mapping[str, Any]) -> Render:
    """``render`` with ``changes`` applied, re-validated by ``schema.Render``.

    Returns a **new** model rather than mutating in place, so a rejected patch
    leaves the caller's object untouched: nothing half-applied, nothing to roll
    back.
    """
    merged = {**render.model_dump(), **dict(changes)}
    try:
        return Render.model_validate(merged)
    except ValidationError as exc:
        raise Invalid(flatten(exc)) from exc


def check_script(script: Any) -> None:
    """Round-trip a Book Script through its own validator before it is saved.

    The one guarantee that makes the bricked-book bug impossible to repeat for any
    field, present or future: what we are about to write is exactly what
    ``load_script`` will accept back.
    """
    from ..schema import BookScript

    try:
        BookScript.model_validate_json(script.model_dump_json())
    except ValidationError as exc:
        raise Invalid(f"this edit would produce a script the studio cannot read back: {flatten(exc)}") from exc


# ---- settings ---------------------------------------------------------------

def check_settings_patch(patch: Mapping[str, Any], catalog: Iterable[EngineOption]) -> None:
    """Settings are global: a bad engine here is silently inherited by every
    book created afterwards, which is why this one is checked
    against the catalog even though the line-level PATCH already is."""
    options = list(catalog)
    for field in ("narrator_engine", "expressive_engine"):
        value = patch.get(field)
        if value:
            try:
                engine_option(str(value), options)
            except Invalid as exc:
                raise Invalid(f"{field}: {exc}") from exc
    director = patch.get("director")
    if isinstance(director, Mapping) and director.get("provider") not in (None, "ollama", "gemini"):
        raise Invalid("director.provider must be 'ollama' or 'gemini'")
