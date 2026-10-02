"""Renderer/generate tests — fully offline via the fake synth (no gateway)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import wave

import pytest

from pipeline.build import build_book_script
from pipeline.generate import generate
from pipeline.render import Renderer, RenderStopped, fake_synth, http_synth
from pipeline.schema import BookScript, Render

_SAMPLE = "Chapter One\n\nAlpha here. Beta too.\n\n* * *\n\nGamma alone.\n\nChapter Two\n\nOmega."


def _script(tmp_path):
    p = tmp_path / "b.txt"
    p.write_text(_SAMPLE, encoding="utf-8")
    return build_book_script(str(p), title="T")


def _wav_ms(path):
    with wave.open(path, "rb") as w:
        return round(w.getnframes() / w.getframerate() * 1000)


def test_render_writes_chapter_wavs_and_timestamps(tmp_path):
    script = _script(tmp_path)
    out = tmp_path / "out"
    Renderer(fake_synth(), cache_dir=str(out / ".cache")).render(script, str(out))

    # Every chapter got an audio file that actually exists on disk.
    for c in script.chapters:
        assert c.audio_file and (out / c.audio_file).exists()
        assert c.duration_ms > 0

    # Every utterance got an AudioSpan, and spans are ordered & non-overlapping.
    spans = [u.audio for u in script.utterances if u.chapter_index == 0]
    assert all(s is not None for s in spans)
    for a, b in zip(spans, spans[1:]):
        assert a.end_ms <= b.start_ms  # trail pause sits between them

    # The chapter file is at least as long as its last utterance's end.
    ch0 = script.chapters[0]
    assert _wav_ms(str(out / ch0.audio_file)) >= spans[-1].end_ms


def test_cache_avoids_resynthesizing(tmp_path):
    script = _script(tmp_path)
    out = tmp_path / "out"
    calls = {"n": 0}
    base = fake_synth()

    def counting(text, render: Render):
        calls["n"] += 1
        return base(text, render)

    Renderer(counting, cache_dir=str(out / ".cache")).render(script, str(out))
    first = calls["n"]
    assert first == len(script.utterances)

    # Re-render a fresh script with the same cache dir → zero new synth calls.
    script2 = _script(tmp_path)
    Renderer(counting, cache_dir=str(out / ".cache")).render(script2, str(out / "out2"))
    assert calls["n"] == first  # all served from cache


def test_generate_offline_emits_playable_folder(tmp_path):
    src = tmp_path / "b.txt"
    src.write_text(_SAMPLE, encoding="utf-8")
    out = tmp_path / "book"
    generate(str(src), str(out), title="T", offline=True, progress=False)

    assert (out / "manifest.json").exists()
    assert (out / "player.html").exists()
    assert (out / "chapter_000.wav").exists()

    manifest = BookScript.model_validate_json((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.chapters[0].audio_file == "chapter_000.wav"
    assert manifest.utterances[0].audio is not None


# ---- direction reaches the engine, and the cache honours it -----------------
# The bug this guards: Render grew an `instruction`, but the cache key hashed
# only (schema, engine, voice, speed, text). Re-render a book with a new
# performance note and every line would be served from the old cache — the audio
# is stale and sounds completely fine, so nothing tells you it didn't take.


def test_undirected_cache_key_matches_the_pre_direction_formula(tmp_path):
    """Adding direction must not invalidate every existing book's cache. With no
    instruction set, the key has to hash exactly as it did before the field
    existed — otherwise shipping this re-synthesizes every book anyone owns."""
    legacy = hashlib.sha256(
        "0.1.0\x00kokoro\x00af_heart\x001.0\x00Hello.".encode("utf-8")
    ).hexdigest()[:32]
    renderer = Renderer(fake_synth(), cache_dir=str(tmp_path / "c"))
    key = renderer._cache_key("Hello.", Render(engine="kokoro", voice_id="af_heart"), "0.1.0")
    assert key == legacy


def test_changing_direction_forces_a_resynth(tmp_path):
    renderer = Renderer(fake_synth(), cache_dir=str(tmp_path / "c"))
    base = Render(engine="breeze", voice_id=None, instruction="Slow and restrained.")
    other = Render(engine="breeze", voice_id=None, instruction="Bright and quick.")
    none_ = Render(engine="breeze", voice_id=None)

    k_base = renderer._cache_key("It was over.", base, "0.1.0")
    assert renderer._cache_key("It was over.", other, "0.1.0") != k_base
    assert renderer._cache_key("It was over.", none_, "0.1.0") != k_base
    # Same direction, same key — direction doesn't defeat caching, it scopes it.
    assert renderer._cache_key("It was over.", base.model_copy(), "0.1.0") == k_base


def test_editing_direction_re_renders_only_that_line(tmp_path):
    """The point of the content hash: change one line's note, pay for one line."""
    script = _script(tmp_path)
    out = tmp_path / "out"
    calls = {"n": 0}
    base = fake_synth()

    def counting(text, render: Render):
        calls["n"] += 1
        return base(text, render)

    cache = str(out / ".cache")
    Renderer(counting, cache_dir=cache).render(script, str(out))
    first = calls["n"]

    script2 = _script(tmp_path)
    script2.utterances[1].render.instruction = "Almost a whisper."
    Renderer(counting, cache_dir=cache).render(script2, str(out / "out2"))
    assert calls["n"] == first + 1  # exactly one line re-synthesized


def test_spoken_text_goes_to_the_engine_but_not_to_the_reader(tmp_path):
    """Inline vocal events belong in the audio, never in the transcript — the
    read-along player renders Utterance.text, so a tag there would be printed."""
    script = _script(tmp_path)
    tagged = "(sigh) " + script.utterances[0].text
    display = script.utterances[0].text
    script.utterances[0].render.spoken_text = tagged

    seen: list[str] = []
    base = fake_synth()

    def recording(text, render: Render):
        seen.append(text)
        return base(text, render)

    Renderer(recording, cache_dir=str(tmp_path / "c")).render(script, str(tmp_path / "o"))
    assert seen[0] == tagged                      # engine hears the tag
    assert script.utterances[0].text == display   # reader never sees it


def test_spoken_text_is_part_of_the_cache_key(tmp_path):
    renderer = Renderer(fake_synth(), cache_dir=str(tmp_path / "c"))
    plain = renderer._cache_key("It was over.", Render(engine="breeze"), "0.1.0")
    tagged = renderer._cache_key("(sigh) It was over.", Render(engine="breeze"), "0.1.0")
    assert plain != tagged


def test_http_synth_sends_instruction_and_routes_to_the_utterances_engine(monkeypatch):
    """A note is useless if it goes to an engine that can't take one, so the
    request must follow Render.engine rather than one fixed engine."""
    seen: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"RIFFfake"

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    synth = http_synth("http://gw", engine="kokoro")

    synth("(sigh) Hello.", Render(engine="breeze", instruction="Slow and restrained."))
    assert seen["url"].endswith("/api/synthesize/breeze")  # not the "kokoro" fallback
    assert seen["body"]["text"] == "(sigh) Hello."
    assert seen["body"]["instruction"] == "Slow and restrained."

    # With no note the field is omitted rather than sent as null — engines that
    # don't support it should not receive the key at all.
    synth("Hello.", Render(engine="kokoro"))
    assert seen["url"].endswith("/api/synthesize/kokoro")
    assert "instruction" not in seen["body"]


# ---- what the studio's job layer needs from the renderer --------------------


def test_render_can_be_limited_to_some_chapters(tmp_path):
    """A "render the stale chapters" job must leave the rest of the book alone:
    not re-stitched, not re-normalized, and above all not stripped of the
    timings the player reads."""
    script = _script(tmp_path)
    out = tmp_path / "out"
    cache = str(out / ".cache")
    Renderer(fake_synth(), cache_dir=cache).render(script, str(out))

    ch1_path = out / script.chapters[1].audio_file
    before_mtime = ch1_path.stat().st_mtime_ns
    before_spans = {
        u.id: (u.audio.start_ms, u.audio.end_ms)
        for u in script.utterances if u.chapter_index == 1
    }
    before_duration = script.chapters[1].duration_ms

    calls = {"n": 0}
    base = fake_synth()

    def counting(text, render: Render):
        calls["n"] += 1
        return base(text, render)

    # The studio's render job grafts the last manifest's timings onto the
    # working script before a partial render, so the untouched chapters still
    # describe real audio in the manifest it writes afterwards.
    fresh = _script(tmp_path)
    spans = {u.id: u.audio for u in script.utterances if u.audio is not None}
    for u in fresh.utterances:
        u.audio = spans.get(u.id)
    for chapter, previous in zip(fresh.chapters, script.chapters):
        chapter.audio_file, chapter.duration_ms = previous.audio_file, previous.duration_ms
    fresh.utterances[0].render.instruction = "Slower."

    Renderer(counting, cache_dir=cache).render(fresh, str(out), chapters=[0])

    assert calls["n"] == 1                       # only the edited line, only chapter 0
    assert ch1_path.stat().st_mtime_ns == before_mtime  # chapter 1's WAV untouched
    assert fresh.chapters[1].audio_file == script.chapters[1].audio_file
    assert fresh.chapters[1].duration_ms == before_duration
    assert {
        u.id: (u.audio.start_ms, u.audio.end_ms)
        for u in fresh.utterances if u.chapter_index == 1
    } == before_spans
    # Chapter 0 really was re-stitched around the new take.
    assert fresh.chapters[0].duration_ms > 0
    assert (out / fresh.chapters[0].audio_file).stat().st_mtime_ns > before_mtime


def test_on_line_reports_cache_hits_synth_calls_and_seconds(tmp_path):
    script = _script(tmp_path)
    out = tmp_path / "out"
    cache = str(out / ".cache")
    seen: list[tuple[str, str, bool]] = []

    def record(u, key, cached, seconds):
        seen.append((u.id, key, cached))
        assert seconds >= 0.0

    renderer = Renderer(fake_synth(), cache_dir=cache)
    renderer.render(script, str(out), on_line=record)
    assert [row[0] for row in seen] == [u.id for u in script.utterances]
    assert all(row[1] and len(row[1]) == 32 for row in seen)
    assert not seen[0][2]  # first pass: a real synth call

    seen.clear()
    Renderer(fake_synth(), cache_dir=cache).render(_script(tmp_path), str(out / "again"), on_line=record)
    assert all(row[2] for row in seen)  # second pass: every line served from cache


def test_should_stop_raises_and_keeps_what_was_already_synthesized(tmp_path):
    """A stop must not look like a finished render — the caller would write a
    manifest for a half-rendered book."""
    script = _script(tmp_path)
    out = tmp_path / "out"
    cache = str(out / ".cache")
    done = {"n": 0}

    def stop():
        return done["n"] >= 2

    def counter(u, key, cached, seconds):
        done["n"] += 1

    renderer = Renderer(fake_synth(), cache_dir=cache)
    with pytest.raises(RenderStopped):
        renderer.render(script, str(out), on_line=counter, should_stop=stop)
    assert done["n"] == 2

    # Resuming is cheap: the two takes already synthesized are in the cache.
    hits = {"n": 0}

    def count_hits(u, key, cached, seconds):
        hits["n"] += int(cached)

    Renderer(fake_synth(), cache_dir=cache).render(_script(tmp_path), str(out), on_line=count_hits)
    assert hits["n"] >= 2


def test_render_line_and_cache_path_agree_with_a_full_render(tmp_path):
    script = _script(tmp_path)
    out = tmp_path / "out"
    renderer = Renderer(fake_synth(), cache_dir=str(out / ".cache"))
    utterance = script.utterances[0]

    key, cached, seconds = renderer.render_line(utterance.text, utterance.render, script.schema_version)
    assert not cached and seconds >= 0.0
    assert os.path.exists(renderer.cache_path(key))

    # The same line rendered by the full pass hits that very file.
    hit = {}

    def record(u, k, c, s):
        if u.id == utterance.id:
            hit["key"], hit["cached"] = k, c

    renderer.render(script, str(out), on_line=record)
    assert hit == {"key": key, "cached": True}

    again = renderer.render_line(utterance.text, utterance.render, script.schema_version)
    assert again[0] == key and again[1] is True


def test_key_for_hashes_the_text_the_engine_will_actually_hear(tmp_path):
    """The bug this pins: ``cache_key`` hashes what it is given, but ``render``
    gives it the text *after* ``text_transform``. Asking "is this cached?" with
    the raw text reports every line of a book with a pronunciation lexicon as
    cached and then re-synthesizes all of them."""
    plain = Renderer(fake_synth(), cache_dir=str(tmp_path / "c"))
    respelled = Renderer(
        fake_synth(),
        cache_dir=str(tmp_path / "c"),
        text_transform=lambda t: t.replace("Elara", "eh-LAR-uh"),
    )
    render = Render(engine="kokoro", voice_id="af_heart")

    assert plain.key_for("Elara waited.", render, "0.1.0") == plain.cache_key(
        "Elara waited.", render, "0.1.0"
    )
    assert respelled.key_for("Elara waited.", render, "0.1.0") == plain.cache_key(
        "eh-LAR-uh waited.", render, "0.1.0"
    )
    # A line the lexicon does not touch keeps its key — and its cached audio.
    assert respelled.key_for("Marcus waited.", render, "0.1.0") == plain.key_for(
        "Marcus waited.", render, "0.1.0"
    )

    key, _cached, _s = respelled.render_line("Elara waited.", render, "0.1.0")
    assert key == respelled.key_for("Elara waited.", render, "0.1.0")


# ---- rule 2: tags only ever reach an engine that advertises them -------------

_BREEZE_TAGS = {"breeze": ["(sigh)", "(laugh)"], "kokoro": []}


def test_spoken_text_is_dropped_for_an_engine_that_cannot_hear_the_tags(tmp_path):
    """Contract rule 2, which nothing enforced: the studio would happily send
    "(sigh) It was over." to Kokoro, which reads the tag out as a word. The
    reader's text is what an engine without that vocabulary gets."""
    renderer = Renderer(fake_synth(), cache_dir=str(tmp_path / "c"), engine_tags=_BREEZE_TAGS)
    spoken = Render(engine="breeze", spoken_text="(sigh) It was over.")
    assert renderer.spoken_for("It was over.", spoken) == "(sigh) It was over."

    on_kokoro = Render(engine="kokoro", spoken_text="(sigh) It was over.")
    assert renderer.spoken_for("It was over.", on_kokoro) == "It was over."

    unknown_engine = Render(engine="not-installed", spoken_text="(sigh) It was over.")
    assert renderer.spoken_for("It was over.", unknown_engine) == "It was over."

    # A tag the engine does not know, on an engine that has some: the annotated
    # passage no longer strips back to the line, so the line wins.
    unknown_tag = Render(engine="breeze", spoken_text="(kazoo) It was over.")
    assert renderer.spoken_for("It was over.", unknown_tag) == "It was over."

    # With no catalog at all (the CLI), the stored value is trusted as before.
    assert Renderer(fake_synth(), cache_dir=str(tmp_path / "d")).spoken_for(
        "It was over.", on_kokoro
    ) == "(sigh) It was over."


def test_the_gate_decides_the_cache_key_and_the_synth_call_together(tmp_path):
    """If the gate and the key disagreed, a gated line would report "cached" and
    then re-synthesize forever — the exact class of bug the key formula exists
    to prevent."""
    script = _script(tmp_path)
    script.utterances[0].render.engine = "kokoro"
    script.utterances[0].render.spoken_text = "(sigh) " + script.utterances[0].text
    renderer = Renderer(fake_synth(), cache_dir=str(tmp_path / "c"), engine_tags=_BREEZE_TAGS)

    seen: list[str] = []
    base = fake_synth()

    def recording(text, render: Render):
        seen.append(text)
        return base(text, render)

    watched = Renderer(recording, cache_dir=str(tmp_path / "c"), engine_tags=_BREEZE_TAGS)
    watched.render(script, str(tmp_path / "o"))
    assert seen[0] == script.utterances[0].text, "kokoro must not hear the tag"
    assert script.utterances[0].audio.cache_key == renderer.key_for(
        renderer.spoken_for(script.utterances[0].text, script.utterances[0].render),
        script.utterances[0].render,
        script.schema_version,
    )


def test_squash_matches_the_directors_copy_of_the_rule():
    """Three copies of one normalization rule exist (gateway → direct.py →
    render.py, each because the next cannot import the previous). Pin them, or
    a fix to one silently changes what the other two accept."""
    from pipeline.direct import _squash as director_squash
    from pipeline.render import _squash as render_squash

    for sample in (
        "again (pause). Outside",
        "  a   b  ",
        "He said “no”, then left .",
        "It was over…",
    ):
        assert render_squash(sample) == director_squash(sample)


# ---- what actually goes on the wire ----------------------------------------
# All three cases below were recorded against a stub gateway.


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"RIFFfake"


def _recorder(monkeypatch):
    seen: list[dict] = []

    def fake_urlopen(req, timeout=None):
        seen.append({"url": req.full_url, "body": json.loads(req.data.decode("utf-8"))})
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def test_a_note_is_not_sent_to_an_engine_that_cannot_take_one(monkeypatch):
    """A manual note typed into the Inspector on a Kokoro line travelled to the
    engine anyway. Kokoro declares ``instruction: false``; the field is noise to
    it, and sending it invites an engine to act on something we never validated
    for it."""
    seen = _recorder(monkeypatch)
    synth = http_synth("http://gw", instruction_engines={"breeze", "indextts2"})

    synth("Hello.", Render(engine="kokoro", instruction="Read this bitterly."))
    assert "instruction" not in seen[-1]["body"]

    synth("Hello.", Render(engine="breeze", instruction="Read this bitterly."))
    assert seen[-1]["body"]["instruction"] == "Read this bitterly."

    # A designed voice is a description, not a note, and is gated the same way:
    # an engine that cannot take an instruction cannot be told about a voice.
    synth("Hello.", Render(engine="kokoro", voice_design="a warm, low voice"))
    assert "instruction" not in seen[-1]["body"]


def test_without_a_catalog_the_stored_value_is_still_trusted(monkeypatch):
    """Same convention as ``Renderer(engine_tags=...)``: no catalog to check
    against means no gate, so the CLI renders exactly as it did before."""
    seen = _recorder(monkeypatch)
    http_synth("http://gw")("Hello.", Render(engine="kokoro", instruction="Bitterly."))
    assert seen[-1]["body"]["instruction"] == "Bitterly."


def test_a_designed_voice_plus_a_full_length_note_stays_inside_the_cap(monkeypatch):
    """``direct._clean_instruction`` and the gateway both cap the *note* at 400,
    then ``http_synth`` prepended the voice description to it — 443 characters
    on the wire in a recorded trace, i.e. every cap upstream was exceeded at
    the last hop. The description must survive whole (it is the character's
    identity, byte-identical on every line, or the engine designs a new voice
    mid-scene); the note is what gets trimmed."""
    from pipeline.render import MAX_INSTRUCTION_CHARS, compose_instruction

    design = "a warm, low adult female voice, unhurried"
    note = "Very " * 80  # 400 chars, exactly what _clean_instruction allows
    composed = compose_instruction(design, note[:400])
    assert len(composed) <= MAX_INSTRUCTION_CHARS
    assert composed.startswith(design + ". ")

    seen = _recorder(monkeypatch)
    http_synth("http://gw")(
        "Hello.", Render(engine="breeze", voice_design=design, instruction=note[:400])
    )
    assert len(seen[-1]["body"]["instruction"]) <= MAX_INSTRUCTION_CHARS


def test_composition_is_the_description_then_the_note_and_neither_twice():
    from pipeline.render import compose_instruction

    assert compose_instruction("a low voice", "Slowly.") == "a low voice. Slowly."
    assert compose_instruction("a low voice", None) == "a low voice"
    assert compose_instruction(None, "Slowly.") == "Slowly."
    assert compose_instruction(None, None) is None
    assert compose_instruction("  ", "") is None


def test_render_skips_chapters_the_book_says_are_not_audio(tmp_path):
    """A copyright page is ``include_in_audio: false`` so it is not read aloud.
    The studio's job layer filtered these; a caller that named no chapters
    (``pipeline.generate``) synthesized and stitched them anyway."""
    script = _script(tmp_path)
    script.chapters[0].include_in_audio = False
    out = tmp_path / "out"

    calls = {"n": 0}
    base = fake_synth()

    def counting(text, render: Render):
        calls["n"] += 1
        return base(text, render)

    Renderer(counting, cache_dir=str(out / ".cache")).render(script, str(out))

    excluded = [u for u in script.utterances if u.chapter_index == 0]
    assert calls["n"] == len(script.utterances) - len(excluded)
    assert script.chapters[0].audio_file is None
    assert not (out / "chapter_000.wav").exists()
    assert script.chapters[1].audio_file and (out / script.chapters[1].audio_file).exists()


def test_naming_an_excluded_chapter_explicitly_still_renders_it(tmp_path):
    """The filter is what "every chapter" means, not a prohibition — a caller
    that asks for chapter 0 by index is asking for chapter 0."""
    script = _script(tmp_path)
    script.chapters[0].include_in_audio = False
    out = tmp_path / "out"
    Renderer(fake_synth(), cache_dir=str(out / ".cache")).render(
        script, str(out), chapters=[0]
    )
    assert script.chapters[0].audio_file and (out / script.chapters[0].audio_file).exists()
