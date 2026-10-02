"""AI director: an LLM reads a passage and returns structured emotional
direction (dominant emotion, intensity, a voice hint, and a per-line breakdown).
Two backends, both over plain HTTP (no SDKs):

- **ollama**  → local models at ``{ollama_url}/api/generate`` (format=json)
- **gemini**  → Google Generative Language REST API (responseMimeType=json)

The result drives the same per-engine controls the manual presets do.

The prompt itself lives in ``gateway/app/prompts/director.md`` (override with
``DIRECTOR_PROMPT_PATH``) and is re-read whenever the file changes, so you can
tune the director's voice without touching Python or restarting anything.

Two of the returned fields are engine-agnostic on purpose: ``scene_note`` is
context for the human and is never sent to an engine, and ``pace`` (0.8-1.2,
clamped here) is a speed multiplier every engine can honour — including the ones
with no emotion input at all, which is the only way a director pass changes what
a Kokoro book sounds like.

**Everything the model returns is treated as untrusted.** ``emotion_label`` and
``voice`` are coerced to that engine's real vocabulary, ``instruction`` is dropped
for engines that can't take one, and a tag-annotated ``text`` is rejected outright
unless it is character-identical to the source once the tags are removed — an LLM
that quietly paraphrases a paragraph would otherwise ship a wrong book.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import NamedTuple

import httpx

from ..models import DirectorRequest

_EMOTIONS = "neutral, calm, happy, sad, angry, afraid, surprised, excited, tender"

_DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "director.md"

# An instruction is a performance note, not an essay. Anything longer is the model
# rambling.
#
# This number is *our* policy, not an engine limit — audited 2026-09-05: Breeze
# documents no length cap on `instruction`, and neither does any other engine
# here. The one real ceiling upstream is a 2048-token budget shared by the
# instruction, the text, and the reference clip's audio tokens, so a runaway note
# competes with the passage itself for room. 400 chars keeps a note comfortably
# inside that without needing a tokenizer on this side.
#
# The older comment claimed long instructions "measurably dilute adherence". That
# was never measured. It may well be true; treat it as untested.
_MAX_INSTRUCTION_CHARS = 400

_prompt_cache: tuple[str, float, str] | None = None  # (path, mtime, template)


def _prompt_path() -> Path:
    override = os.environ.get("DIRECTOR_PROMPT_PATH", "").strip()
    return Path(override) if override else _DEFAULT_PROMPT_PATH


def load_prompt_template() -> str:
    """Read the prompt body from Markdown, cached until the file changes.

    Everything up to and including the first line that is exactly ``---`` is
    editor-facing documentation and is stripped; only the body is sent.
    """
    global _prompt_cache
    path = _prompt_path()
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise ValueError(f"director prompt not found at {path}: {exc}") from exc

    if _prompt_cache and _prompt_cache[0] == str(path) and _prompt_cache[1] == mtime:
        return _prompt_cache[2]

    raw = path.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^---[ \t]*$", raw, maxsplit=1)
    body = (parts[1] if len(parts) > 1 else raw).strip()
    if not body:
        raise ValueError(f"director prompt at {path} is empty below the '---' marker")
    _prompt_cache = (str(path), mtime, body)
    return body


def prompt_version() -> str:
    """Short content hash of the prompt *as it would be sent right now*.

    A book-scale pass caches per scene, and the prompt is a file that anyone can
    edit between runs (that is the whole point of it being a file). Without a
    version in the cache key, tuning ``director.md`` and re-running would serve
    every scene from the cache and look like the edit did nothing. Hashing the
    loaded body — not the mtime, not the path — means a cosmetic re-save keeps
    the cache and a real edit invalidates it.

    Returns ``""`` when the prompt can't be read, so a missing file degrades to
    "no cache reuse" instead of taking down ``/director/defaults``.
    """
    try:
        body = load_prompt_template()
    except (ValueError, OSError):
        return ""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _engine_block(engines: list) -> str:
    if not engines:
        return "(none — return an empty per_engine list)"
    lines = []
    for e in engines:
        if e.emotion_labels:
            emo = ", ".join(e.emotion_labels)
        elif e.emotion_scalar:
            emo = "none (intensity/scalar only)"
        else:
            emo = "none"
        voi = ", ".join(e.voices) if e.voices else "none (cloned from a reference clip)"
        parts = [
            f'- id "{e.id}" ({e.name}): emotions = [{emo}]; voices = [{voi}]',
            f"instruction = {'yes' if e.instruction else 'no'}",
        ]
        # Only advertise a tag vocabulary when the engine actually has one — an
        # empty list must read as "no tags", never as "any tag you like".
        parts.append(
            f"audio tags = [{', '.join(e.audio_tags)}]" if e.audio_tags else "audio tags = none"
        )
        lines.append("; ".join(parts))
    return "\n".join(lines)


def build_prompt(req: DirectorRequest) -> str:
    return (
        load_prompt_template()
        .replace("{{EMOTIONS}}", _EMOTIONS)
        .replace("{{ENGINE_BLOCK}}", _engine_block(req.engines))
        .replace("{{TEXT}}", req.text)
    )


async def annotate(req: DirectorRequest, ollama_url: str, gemini_key: str) -> dict:
    prompt = build_prompt(req)
    if req.provider == "ollama":
        raw = await _ollama(req.model, prompt, req.ollama_url or ollama_url)
    elif req.provider == "gemini":
        key = req.api_key or gemini_key
        if not key:
            raise ValueError("a Gemini API key is required")
        raw = await _gemini(req.model, prompt, key)
    else:  # pragma: no cover - guarded by the request schema
        raise ValueError(f"unknown provider '{req.provider}'")
    data = _parse(raw)
    data["per_engine"] = _validate_per_engine(
        data.get("per_engine"), req.engines, data.get("scalar", 0.5), req.text
    )
    # Stamped on the reply, not just exposed on /defaults: a book-scale pass
    # records the prompt that produced each scene, so a cached scene and a fresh
    # one can be told apart after the prompt is edited.
    data["prompt_version"] = prompt_version()
    return data


async def _ollama(model: str, prompt: str, base_url: str) -> str:
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
        )
        resp.raise_for_status()
        payload = resp.json()
        # Ollama can return HTTP 200 with an error body (e.g. a model that isn't
        # pulled). Surface that instead of a misleading "empty response" later.
        if payload.get("error"):
            raise ValueError(f"Ollama error: {payload['error']}")
        return payload.get("response", "")


async def _gemini(model: str, prompt: str, api_key: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(
            url,
            params={"key": api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        # A safety-filtered or empty prompt yields no candidates (just
        # promptFeedback). Guard the indexing so we raise a clean ValueError
        # (→ HTTP 400) instead of an unhandled KeyError/IndexError (→ 500).
        candidates = data.get("candidates")
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason") or "no candidates returned"
            raise ValueError(f"Gemini returned no output ({reason})")
        try:
            return candidates[0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"unexpected Gemini response shape: {exc}") from exc


def _parse(raw: str) -> dict:
    """Best-effort extraction of the JSON object from a model's reply."""
    if not raw:
        raise ValueError("empty response from the model")
    text = raw.strip()
    # strip ```json ... ``` fences if present
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model did not return valid JSON: {exc}") from exc

    # normalize / clamp so a sloppy model can't break the response model
    try:
        scalar = float(data.get("scalar", 0.5))
    except (TypeError, ValueError):
        scalar = 0.5
    data["scalar"] = max(0.0, min(1.0, scalar))
    if not isinstance(data.get("segments"), list):
        data["segments"] = []
    data["scene_note"] = _clean_scene_note(data.get("scene_note"))
    data["pace"] = _clean_pace(data.get("pace"))
    return data


_MIN_PACE, _MAX_PACE = 0.8, 1.2


def _clean_pace(value) -> float | None:
    """Clamp the director's pace into the range the schema allows.

    Clamped, not rejected: a model that answers 1.5 wants it faster, and dropping
    the field entirely loses that. Anything unparseable becomes ``None`` — no
    pace asked for, which is different from "read it at 1.0".
    """
    if value is None:
        return None
    try:
        pace = float(value)
    except (TypeError, ValueError):
        return None
    if pace != pace or pace in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return round(max(_MIN_PACE, min(_MAX_PACE, pace)), 3)


def _clean_scene_note(value) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if text.lower() in ("null", "none", "n/a"):
        return ""
    return text[:_MAX_INSTRUCTION_CHARS]


def _pick(value, allowed: list[str]) -> str | None:
    """Return the allowed option matching ``value`` (case-insensitive), else None.
    Stops the LLM from injecting a label/voice the engine doesn't actually have."""
    if value is None:
        return None
    text = str(value).strip().lower()
    for opt in allowed:
        if opt.lower() == text:
            return opt
    return None


def _clean_instruction(value, allowed: bool) -> str | None:
    """Keep a prose note only for engines that declared they accept one."""
    if not allowed or value is None:
        return None
    text = " ".join(str(value).split())
    if not text or text.lower() in ("null", "none", "n/a"):
        return None
    return text[:_MAX_INSTRUCTION_CHARS]


def _squash(text: str) -> str:
    """Normalized form for comparing two versions of a passage.

    Collapses whitespace runs, then closes up space in front of punctuation.
    That second step matters: a model that annotates ``again. Outside`` as
    ``again (pause). Outside`` is completely correct, but removing the tag leaves
    ``again . Outside`` — an artifact of *our* stripping, not a change to the
    author's words. Without this, good annotations get thrown away.

    Applied to both sides, so it never lets a real rewrite through: a paraphrase
    still differs in non-whitespace characters.
    """
    collapsed = " ".join(text.split())
    return re.sub(r"\s+([,.;:!?…)\]}»”’\"'])", r"\1", collapsed)


def _canonicalize_tags(text: str, allowed: list[str]) -> str:
    """Rewrite near-miss tag spellings into the engine's exact form.

    Told to write ``(pause)``, a small local model will cheerfully write
    ``[pause]``, ``<Pause>`` or ``( pause )``. The word is right and the position
    is right; only the punctuation is wrong. Repairing that is what makes a 12B
    model usable as a director instead of a coin flip.

    This is safe by construction: the caller re-checks that stripping the tags
    still reproduces the source exactly, so normalizing can only ever change the
    *tags* — never the author's words. A passage that genuinely contains
    ``[sigh]`` as its own text fails that check and is rejected, which is right.
    """
    for tag in allowed:
        if len(tag) < 3 or tag[0] not in "([<{" or tag[-1] not in ")]>}":
            continue  # not a bracketed tag; nothing to normalize
        inner = re.escape(tag[1:-1].strip())
        pattern = re.compile(r"[(\[<{]\s*" + inner + r"\s*[)\]>}]", re.IGNORECASE)
        text = pattern.sub(tag.replace("\\", "\\\\"), text)
    return text


_BRACKETED = re.compile(r"[(\[<{][^)\]>}\n]{1,40}[)\]>}]")


def _strip_tags(text: str, tags: list[str]) -> str:
    for tag in tags:
        text = text.replace(tag, " ")
    return text


def _first_difference(candidate: str, source: str, width: int = 40) -> str:
    """A short 'here is where it diverges' snippet for the review screen.

    Both sides are squashed first, so the offset points at the first *word*
    difference rather than at a space the tag-stripping left behind.
    """
    a, b = _squash(candidate), _squash(source)
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    lead = max(0, i - 12)
    got, want = a[lead : i + width], b[lead : i + width]
    if not got:
        return "the annotated passage stops early"
    return f"…{got}… where the book has …{want}…"


class TagVerdict(NamedTuple):
    """Why a candidate annotation was kept or thrown away.

    ``status`` mirrors the wire contract's ``text_status``:
    ``kept`` (usable), ``rejected`` (the model changed the book — keep it for the
    diff), ``none`` (nothing to apply: no tag vocabulary, no answer, or no tags
    actually inserted).
    """

    status: str  # "kept" | "rejected" | "none"
    text: str | None  # the annotation to use, only when kept
    rejected_text: str | None  # the model's canonicalized candidate, only when rejected
    reason: str | None  # human-readable "why", for the review screen and try_director


def inspect_tagged_text(value, source: str, allowed_tags: list[str]) -> TagVerdict:
    """Judge a tag-annotated passage and say *why*.

    The rule is unchanged and non-negotiable: removing every allowed tag must
    reproduce the source exactly (ignoring whitespace runs). What is new is that
    a failure is no longer silent — the candidate survives in ``rejected_text``
    and ``reason`` names which of the three failure modes it was, because at book
    scale "37 scenes rejected" is unactionable without knowing whether the model
    is paraphrasing, inventing tags, or truncating.
    """
    if not allowed_tags:
        return TagVerdict("none", None, None, "engine declares no audio tags")
    if value is None or not isinstance(value, str):
        return TagVerdict("none", None, None, None)
    candidate = _canonicalize_tags(value.strip(), allowed_tags)
    if not candidate:
        return TagVerdict("none", None, None, None)

    sq_source = _squash(source)
    if _squash(_strip_tags(candidate, allowed_tags)) == sq_source:
        if _squash(candidate) == sq_source:
            # No tag actually landed. Not a failure — the model is allowed to
            # decide a scene needs none — so it must not count as a rejection.
            return TagVerdict("none", None, None, "no tags were inserted")
        return TagVerdict("kept", candidate, None, None)

    # It failed. Work out which way, most specific first.
    unknown = [m for m in _BRACKETED.findall(candidate) if m not in allowed_tags]
    if unknown:
        without_unknown = candidate
        for span in unknown:
            without_unknown = without_unknown.replace(span, " ")
        if _squash(_strip_tags(without_unknown, allowed_tags)) == sq_source:
            listed = ", ".join(dict.fromkeys(unknown))
            return TagVerdict(
                "rejected", None, candidate, f"used a tag the engine does not know: {listed}"
            )

    stripped = _squash(_strip_tags(candidate, allowed_tags))
    if stripped and sq_source.startswith(stripped):
        pct = round(100 * len(stripped) / max(1, len(sq_source)))
        return TagVerdict(
            "rejected", None, candidate, f"truncated: only the first ~{pct}% of the passage came back"
        )
    return TagVerdict(
        "rejected", None, candidate, f"not the source passage: {_first_difference(candidate, source)}"
    )


def _validate_tagged_text(value, source: str, allowed_tags: list[str]) -> str | None:
    """Accept a tag-annotated passage only if it is the *same words* as ``source``.

    Removing every allowed tag must reproduce the source exactly (ignoring
    whitespace runs). That single check catches the three ways this goes wrong:
    a paraphrase, a tag the engine doesn't know, and a truncated passage — any of
    which would put words in the book that the author never wrote.

    Thin wrapper over :func:`inspect_tagged_text`, kept because "did this survive
    validation?" is the question almost every caller actually has.
    """
    return inspect_tagged_text(value, source, allowed_tags).text


def _validate_per_engine(
    raw_list, engines: list, default_scalar: float, source_text: str = ""
) -> list[dict]:
    """Keep only entries for known engines, coerce emotion_label/voice to that
    engine's real options, drop instructions/tags the engine can't use, and
    ensure every engine gets exactly one entry."""
    specs = {e.id: e for e in engines}
    by_id: dict[str, dict] = {}
    if isinstance(raw_list, list):
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            eid = item.get("engine_id")
            spec = specs.get(eid)
            if spec is None or eid in by_id:
                continue
            try:
                scalar = float(item.get("scalar", default_scalar))
            except (TypeError, ValueError):
                scalar = default_scalar
            verdict = inspect_tagged_text(item.get("text"), source_text, spec.audio_tags)
            by_id[eid] = {
                "engine_id": eid,
                "emotion_label": _pick(item.get("emotion_label"), spec.emotion_labels),
                "voice": _pick(item.get("voice"), spec.voices),
                "scalar": max(0.0, min(1.0, scalar)),
                "instruction": _clean_instruction(item.get("instruction"), spec.instruction),
                "text": verdict.text,
                # A rejection used to vanish here, which made a book-scale pass
                # unreviewable: you knew a scene failed but not what the model
                # had written. The candidate now rides along so the review screen
                # can show the diff — it is never used as audio.
                "rejected_text": verdict.rejected_text,
                "text_status": verdict.status,
                "text_reason": verdict.reason,
            }
    # fallback: an entry for every requested engine
    for e in engines:
        by_id.setdefault(
            e.id,
            {
                "engine_id": e.id,
                "emotion_label": None,
                "voice": None,
                "scalar": max(0.0, min(1.0, default_scalar)),
                "instruction": None,
                "text": None,
                "rejected_text": None,
                "text_status": "none",
                "text_reason": None,
            },
        )
    return [by_id[e.id] for e in engines]
