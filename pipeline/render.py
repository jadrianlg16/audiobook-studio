"""Render + master: a Book Script -> per-chapter WAV files + a timestamped
manifest (the same BookScript, now with ``AudioSpan`` on every utterance and
``audio_file``/``duration_ms`` on every chapter).

Design points:
- **Content-hash cache.** Each utterance is keyed by sha256(schema + engine +
  voice + speed + spoken text + direction). Re-running only re-synthesizes what
  changed — swap one character's voice and just their lines re-render. Direction
  is part of the key precisely because two takes that differ *only* in direction
  are different audio; leaving it out would silently serve the first take
  forever, and the failure is inaudible — you would just never hear the note
  take effect.
- **Engine-agnostic.** The renderer takes a ``synth_fn(text, render) -> wav_bytes``
  callable. The default hits the gateway over HTTP; tests inject a fake so the
  whole pipeline runs with no network and no model.
- **Pauses become silence.** ``lead_pause_ms``/``trail_pause_ms`` are rendered as
  zero-PCM between clips so prosody doesn't run together.
- Dependency-light: stdlib ``wave`` for audio, ``urllib`` for HTTP. ``audioop``
  is used for fast gain when present, with a pure-Python fallback.
"""

from __future__ import annotations

import array
import hashlib
import io
import json
import os
import re
import time
import urllib.request
import wave
from collections.abc import Callable, Iterable, Mapping, Sequence

from .schema import AudioSpan, BookScript, Render, Utterance

try:  # audioop is fast C, but removed in Python 3.13 — fall back if absent.
    import audioop
except Exception:  # noqa: BLE001
    audioop = None  # type: ignore[assignment]

SynthFn = Callable[[str, Render], bytes]

# ``on_line(utterance, cache_key, cached, synth_seconds)`` — called after every
# utterance so a caller can count cache hits vs. real synth calls and time the
# latter. ``synth_seconds`` is 0.0 for a cache hit, which is what makes an
# honest RTF possible (synth wall time / audio seconds produced).
OnLine = Callable[[Utterance, str, bool, float], None]

DEFAULT_GATEWAY = os.environ.get("AUDIOBOOK_GATEWAY", "http://localhost:8080")
_TARGET_PEAK = 0.89  # ~ -1 dBFS, leaves headroom


class RenderStopped(RuntimeError):
    """``should_stop()`` returned True between two lines.

    Raised rather than returned so a stop can never be mistaken for a finished
    render: a partial book that silently returns would be written out as a
    complete manifest. Cache entries for the lines already synthesized survive,
    so resuming costs only what was left."""


def _squash(text: str) -> str:
    """Whitespace-normalized form for "is this the same passage?".

    The same rule as ``direct._squash`` (which is itself the gateway's rule),
    re-stated rather than imported because ``direct`` imports *this* module —
    importing back would be a cycle. ``pipeline/tests/test_render.py`` pins the
    two against each other so a change to one fails a test instead of silently
    letting a tagged line through a check the director would have rejected.
    """
    collapsed = " ".join(text.split())
    return re.sub(r"\s+([,.;:!?…)\]}»”’\"'])", r"\1", collapsed)


# ---- WAV helpers ------------------------------------------------------------

def _read_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        pcm = w.readframes(n)
    if sw != 2:
        raise ValueError(f"expected 16-bit PCM, got {sw * 8}-bit")
    if ch == 2:  # engines here are mono, but downmix defensively
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5) if audioop else _downmix(pcm)
    return pcm, sr


def _write_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _downmix(pcm: bytes) -> bytes:
    a = array.array("h")
    a.frombytes(pcm)
    out = array.array("h", [0] * (len(a) // 2))
    for i in range(len(out)):
        out[i] = (a[2 * i] + a[2 * i + 1]) // 2
    return out.tobytes()


def _silence(ms: int, sample_rate: int) -> bytes:
    return b"\x00\x00" * int(sample_rate * ms / 1000)


def _peak_normalize(pcm: bytes, target: float = _TARGET_PEAK) -> bytes:
    if not pcm:
        return pcm
    limit = int(target * 32767)
    if audioop is not None:
        peak = audioop.max(pcm, 2)
        if peak == 0:
            return pcm
        return audioop.mul(pcm, 2, limit / peak)
    a = array.array("h")
    a.frombytes(pcm)
    peak = max((abs(x) for x in a), default=0)
    if peak == 0:
        return pcm
    scale = limit / peak
    for i in range(len(a)):
        a[i] = max(-32768, min(32767, int(a[i] * scale)))
    return a.tobytes()


def _duration_ms(pcm: bytes, sample_rate: int) -> int:
    return round(len(pcm) / 2 / sample_rate * 1000)


# ---- synth backends ---------------------------------------------------------

MAX_INSTRUCTION_CHARS = 400
"""Ceiling on the instruction that actually goes on the wire.

``direct._clean_instruction`` and the gateway's director both cap the *note* at
400 characters (a budget of our own: an engine's prompt shares one token
ceiling between the note and the text). But
a designed voice prepends its description to that note, so the string an engine
receives was ``len(voice_design) + 2 + 400`` — 443 characters in a trace
recorded against a stub gateway, i.e. every cap upstream was quietly exceeded at the last
hop. Composing here is what put it over the line, so trimming here is what puts
it back: the voice description is kept whole (it is the character's identity and
must be byte-identical on every line, or the engine designs a new voice
mid-scene) and the note is trimmed to fit.
"""


def compose_instruction(
    voice_design: str | None,
    instruction: str | None,
    *,
    limit: int = MAX_INSTRUCTION_CHARS,
) -> str | None:
    """The single string an instruction-capable engine is asked to perform.

    ``"<voice_design>. <note>"`` — the description leads so the engine reads the
    pair as one instruction about one speaker, and never both fields separately.
    ``None`` when there is nothing to say.
    """
    design = (voice_design or "").strip()
    note = (instruction or "").strip()
    if design and note:
        composed = f"{design}. {note}"
    else:
        composed = design or note
    if not composed:
        return None
    if len(composed) <= limit:
        return composed
    if design and note and len(design) + 2 < limit:
        room = limit - len(design) - 2  # the ". " that joins them
        return f"{design}. {note[:room].rstrip()}"
    return composed[:limit].rstrip()


def http_synth(
    gateway_url: str = DEFAULT_GATEWAY,
    engine: str = "kokoro",
    timeout: float = 600.0,
    *,
    instruction_engines: Iterable[str] | None = None,
) -> SynthFn:
    """Synthesize by calling the running gateway. No third-party deps.

    ``engine`` is only the **fallback**: each utterance is routed to its own
    ``Render.engine``. That already had to be true — ``Render.engine`` is in the
    cache key and ``CastEntry`` carries one per character — but the request used
    to go to a single fixed engine regardless, so a hybrid book (plain narrator
    on Kokoro, directed dialogue on Breeze) would have quietly sent every line to
    the same place. Today ``generate()`` passes the same value to the builder and
    to here, so this changes nothing until a script actually mixes engines.

    ``instruction_engines`` is the set of engine ids that declare
    ``Capabilities.instruction``. Given one, a note (or a voice description) is
    **not** sent to an engine outside it: Kokoro has no use for the field, and a
    manual note typed into the Inspector on a Kokoro line used to travel to the
    engine anyway. None = no catalog to check against, so the stored value is
    trusted — the same convention as ``Renderer(engine_tags=...)``, and what
    keeps the CLI and the tests rendering exactly as before.
    """
    capable = None if instruction_engines is None else set(instruction_engines)

    def synth(text: str, render: Render) -> bytes:
        payload: dict = {"text": text, "voice": render.voice_id, "speed": render.speed}
        # A designed voice (no named voice_id) must be described on every line,
        # or the engine invents a fresh voice per line instead of a consistent
        # character. When both are set, the voice description leads and the
        # performance note follows, so the engine reads it as one instruction.
        target = render.engine or engine
        if capable is None or target in capable:
            composed = compose_instruction(render.voice_design, render.instruction)
            if composed:
                payload["instruction"] = composed
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{gateway_url.rstrip('/')}/api/synthesize/{render.engine or engine}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    return synth


def fake_synth(sample_rate: int = 24000, wpm: float = 155.0) -> SynthFn:
    """Offline stand-in: returns a valid WAV whose length tracks the word count."""

    def synth(text: str, _render: Render) -> bytes:
        words = max(1, len(text.split()))
        ms = int(words / wpm * 60_000)
        pcm = b"\x20\x00" * int(sample_rate * ms / 1000)  # low, non-zero tone
        return _write_wav(pcm, sample_rate)

    return synth


# ---- renderer ---------------------------------------------------------------

class Renderer:
    def __init__(
        self,
        synth_fn: SynthFn,
        cache_dir: str,
        *,
        normalize: bool = True,
        progress: bool = False,
        text_transform: Callable[[str], str] | None = None,
        engine_tags: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self._synth = synth_fn
        self._cache_dir = cache_dir
        self._normalize = normalize
        self._progress = progress
        # ``{engine_id: audio_tags}``. Given one, the renderer refuses to send a
        # tagged ``spoken_text`` to an engine that does not advertise those tags
        # (contract rule 2) and speaks the reader's ``text`` instead — Kokoro
        # reads "(sigh)" aloud as a word, and nothing downstream would catch it.
        # None = no catalog to check against, so the stored value is trusted
        # (the CLI and the tests render this way).
        self._engine_tags = dict(engine_tags) if engine_tags is not None else None
        # Applied to the spoken text before the cache key is computed and
        # before synth (e.g. a pronunciation lexicon). Doing it here — not
        # at the call site — is what makes a lexicon edit invalidate exactly
        # the lines it touches: the transformed text feeds the same key
        # formula the cache already keys on, so a changed respelling is a
        # changed key is a stale line, with no separate staleness bookkeeping.
        self._text_transform = text_transform
        os.makedirs(cache_dir, exist_ok=True)

    def cache_key(self, text: str, render: Render, schema_version: str) -> str:
        h = hashlib.sha256()
        h.update(
            f"{schema_version}\x00{render.engine}\x00{render.voice_id}\x00"
            f"{render.speed}\x00{text}".encode("utf-8")
        )
        # Appended only when direction is actually set, so every book rendered
        # before this field existed keeps its cache entries instead of
        # re-synthesizing from scratch. (Bumping SCHEMA_VERSION would have been
        # the lazy way to express "the key changed" and would have thrown away
        # every cache in existence, including the undirected ones.)
        if render.instruction:
            h.update(f"\x00instruction={render.instruction}".encode("utf-8"))
        # Same reasoning, same pattern, for voice_design (hybrid casting):
        # only a character moved onto an engine with no named voices ever sets
        # it, so a book cast entirely with named voices keeps its pre-casting cache.
        if render.voice_design:
            h.update(f"\x00voice_design={render.voice_design}".encode("utf-8"))
        return h.hexdigest()[:32]

    _cache_key = cache_key  # back-compat alias; existing call sites use the private name

    def key_for(self, text: str, render: Render, schema_version: str) -> str:
        """The key an utterance will actually be stored under.

        ``cache_key`` hashes the string it is handed; ``render()`` hands it the
        text *after* ``text_transform``. Anything that asks "is this line
        rendered?" has to ask the same question the renderer will — hashing the
        raw text instead would report every line of a book with a pronunciation
        lexicon as cached and then re-synthesize all of them.
        """
        return self.cache_key(
            text if self._text_transform is None else self._text_transform(text),
            render,
            schema_version,
        )

    def spoken_for(self, text: str, render: Render) -> str:
        """What this engine will actually be asked to say.

        One place, because the answer feeds the cache key: if ``render()``
        decided differently from whatever computes ``audio_state``, every line
        with a tag would report "cached" and then re-synthesize (or worse,
        report stale forever). Rule 2 of the contract lives here.
        """
        spoken = render.spoken_text
        if not spoken:
            return text
        if self._engine_tags is None:
            return spoken
        allowed = self._engine_tags.get(render.engine)
        if not allowed:
            # No vocabulary (or an engine this deployment has never heard of):
            # "only ever sent to an engine whose audio_tags include every tag it
            # contains" is not satisfiable, so the reader's text wins.
            return text
        stripped = spoken
        for tag in sorted({t for t in allowed if t}, key=len, reverse=True):
            stripped = stripped.replace(tag, " ")
        if _squash(stripped) != _squash(text):
            return text  # a tag this engine does not know, or drifted wording
        return spoken

    def cache_path(self, key: str) -> str:
        """Where ``key``'s WAV lives. The studio derives ``audio_state`` from
        whether this file exists, so the path formula has exactly one home."""
        return os.path.join(self._cache_dir, f"{key}.wav")

    def _synth_cached(
        self, text: str, render: Render, schema_version: str
    ) -> tuple[bytes, str, bool, float]:
        key = self.cache_key(text, render, schema_version)
        path = self.cache_path(key)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                return fh.read(), key, True, 0.0
        started = time.monotonic()
        wav = self._synth(text, render)
        seconds = time.monotonic() - started
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(wav)
        os.replace(tmp, path)
        return wav, key, False, seconds

    def render_line(
        self, text: str, render: Render, schema_version: str
    ) -> tuple[str, bool, float]:
        """Render one utterance into the cache and report ``(key, cached, seconds)``.

        The single-line endpoint ("hear it back in seconds") needs exactly the
        cache semantics of a full render and nothing else — same transform, same
        key formula, same file — so it goes through the same helper instead of
        re-deriving any of it.
        """
        spoken = text if self._text_transform is None else self._text_transform(text)
        _wav, key, cached, seconds = self._synth_cached(spoken, render, schema_version)
        return key, cached, seconds

    def render(
        self,
        script: BookScript,
        out_dir: str,
        *,
        chapters: Iterable[int] | None = None,
        on_line: OnLine | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> BookScript:
        """Synthesize the book, write per-chapter WAVs into ``out_dir``, and
        stamp timings back onto ``script`` (making it the player manifest).

        ``chapters`` restricts the work to those chapter indices. A chapter left
        out keeps its existing ``audio_file`` / ``duration_ms`` and its lines
        keep their existing ``AudioSpan`` — its WAV on disk is not touched and
        not rewritten. That is what lets a "render the stale chapters" job leave
        the other forty chapters of a novel exactly as they were instead of
        re-stitching (and re-normalizing) audio nothing changed.

        ``on_line`` observes every utterance; ``should_stop`` is polled before
        each one and raises ``RenderStopped`` (cooperative pause/cancel).
        """
        os.makedirs(out_dir, exist_ok=True)
        wanted = None if chapters is None else set(chapters)
        # "Every chapter" means every chapter the book says is audio. A copyright
        # page and a dedication are marked ``include_in_audio: false`` by ingest
        # precisely so they are not read aloud, and the studio's job layer
        # filters them before calling in — but a caller that named no chapters
        # (``pipeline.generate``) rendered and stitched them anyway: 5 of the 48
        # synth calls in a recorded stub-gateway trace were the front matter
        # of hard-manuscript.txt, and the first thing the book said out loud was
        # its own ISBN. An *explicit* chapter list still wins — asking for
        # chapter 0 by index is asking for chapter 0.
        if wanted is None:
            selected = [c for c in script.chapters if getattr(c, "include_in_audio", True)]
        else:
            selected = [c for c in script.chapters if c.index in wanted]
        included = {c.index for c in selected}
        total = sum(1 for u in script.utterances if u.chapter_index in included)
        done = 0

        for chapter in selected:
            utterances = [u for u in script.utterances if u.chapter_index == chapter.index]
            parts: list[bytes] = []
            cursor = 0
            sample_rate = 24000

            for u in utterances:
                if should_stop is not None and should_stop():
                    raise RenderStopped(
                        f"stopped before {u.id} ({done}/{total} lines rendered)"
                    )
                # The engine may be given a different string than the reader sees
                # (inline vocal events). It feeds the cache key too, so editing
                # the annotation re-renders exactly that line.
                spoken = self.spoken_for(u.text, u.render)
                if self._text_transform is not None:
                    spoken = self._text_transform(spoken)
                wav, key, cached, seconds = self._synth_cached(
                    spoken, u.render, script.schema_version
                )
                pcm, sample_rate = _read_pcm(wav)
                lead = _silence(u.render.lead_pause_ms, sample_rate)
                trail = _silence(u.render.trail_pause_ms, sample_rate)

                start_ms = cursor + u.render.lead_pause_ms
                dur = _duration_ms(pcm, sample_rate)
                end_ms = start_ms + dur

                parts.extend((lead, pcm, trail))
                u.audio = AudioSpan(
                    start_ms=start_ms, end_ms=end_ms, sample_rate=sample_rate, cache_key=key
                )
                cursor = end_ms + u.render.trail_pause_ms

                done += 1
                if on_line is not None:
                    on_line(u, key, cached, seconds)
                if self._progress:
                    tag = "cache" if cached else "synth"
                    print(f"  [{done}/{total}] {tag} {u.id} ({dur} ms)", flush=True)

            chapter_pcm = b"".join(parts)
            if self._normalize:
                chapter_pcm = _peak_normalize(chapter_pcm)
            fname = f"chapter_{chapter.index:03d}.wav"
            with open(os.path.join(out_dir, fname), "wb") as fh:
                fh.write(_write_wav(chapter_pcm, sample_rate))
            chapter.audio_file = fname
            chapter.duration_ms = cursor

        return script
