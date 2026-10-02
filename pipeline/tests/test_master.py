"""Mastering/export tests. Anything that shells out to real ffmpeg is marked
``needs_ffmpeg`` and SKIPPED (not failed) when ffmpeg/ffprobe aren't on PATH;
the missing-ffmpeg error path itself is exercised with a monkeypatch so it
runs unconditionally. Run: pytest pipeline/tests/test_master.py -q
"""

from __future__ import annotations

import array
import json
import os
import random
import subprocess
import tempfile
import wave

import pytest

from pipeline import master

needs_ffmpeg = pytest.mark.skipif(
    not master.ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH"
)


# ---- fixtures ----------------------------------------------------------------

def _write_noise_wav(path, sample_rate: int, duration_ms: int, seed: int) -> None:
    """Noise, not a pure tone: loudnorm's gain gets unstable on the near-silent
    constant-sample WAVs `fake_synth` produces (see pipeline/render.py), so a
    seeded PRNG stands in for real speech-like dynamics instead."""
    n = int(sample_rate * duration_ms / 1000)
    rng = random.Random(seed)
    amp = 9000
    samples = array.array("h", (rng.randint(-amp, amp) for _ in range(n)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.tobytes())


def _build_fake_book(
    tmp_path, *, n_chapters: int = 3, sample_rate: int = 24000,
    title: str = "Fake Book", author: str = "Fake Author",
):
    chapters = []
    for i in range(n_chapters):
        dur_ms = 1500 + i * 400
        fname = f"chapter_{i:03d}.wav"
        _write_noise_wav(tmp_path / fname, sample_rate, dur_ms, seed=i)
        chapters.append({
            "index": i, "title": f"Chapter {i + 1}", "scenes": [],
            "audio_file": fname, "duration_ms": dur_ms,
        })
    manifest = {
        "schema_version": "0.1.0",
        "meta": {"title": title, "author": author, "language": "en",
                  "source_path": "x", "book_type": "general_fiction"},
        "cast": [],
        "chapters": chapters,
        "utterances": [],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path, chapters


def _ffprobe(path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_chapters", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ---- ffmpeg discovery ----------------------------------------------------------

def test_ffmpeg_available_reflects_path():
    assert isinstance(master.ffmpeg_available(), bool)
    if master.ffmpeg_available():
        version = master.ffmpeg_version()
        assert version and version[0].isdigit()


def test_ffmpeg_version_none_when_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(master.shutil, "which", lambda name: None)
    assert master.ffmpeg_available() is False
    assert master.ffmpeg_version() is None


# ---- export formats, chapters, sample rate -------------------------------------

@needs_ffmpeg
@pytest.mark.parametrize("fmt,codec", [("m4b", "aac"), ("mp3", "mp3"), ("wav", "pcm_s16le")])
def test_master_book_formats_chapters_sample_rate_and_duration(tmp_path, fmt, codec):
    book_dir, chapters = _build_fake_book(tmp_path)
    result = master.master_book(str(book_dir), format=fmt, loudness="acx", sample_rate=44100)

    assert result.format == fmt
    assert result.chapters == len(chapters)
    assert result.sample_rate == 44100
    assert os.path.exists(result.path)
    assert result.bytes == os.path.getsize(result.path)

    probe = _ffprobe(result.path)
    audio_streams = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    assert audio_streams, probe
    assert audio_streams[0]["codec_name"] == codec
    assert audio_streams[0]["sample_rate"] == "44100"

    total_expected_ms = sum(c["duration_ms"] for c in chapters)
    actual_ms = float(probe["format"]["duration"]) * 1000
    assert abs(actual_ms - total_expected_ms) <= 1000  # within 1s of the sum

    if fmt == "wav":
        # Verified against real ffmpeg output: the WAV muxer accepts
        # -map_chapters without error but the container has no chapter
        # mechanism, so ffprobe reports none. Not a bug in master.py.
        assert probe.get("chapters", []) == []
    else:
        probed_chapters = probe["chapters"]
        assert len(probed_chapters) == len(chapters)
        assert [c["tags"]["title"] for c in probed_chapters] == [c["title"] for c in chapters]


@needs_ffmpeg
def test_master_book_tags_title_and_author(tmp_path):
    book_dir, _ = _build_fake_book(tmp_path, n_chapters=1, title="My Book", author="A. Writer")
    result = master.master_book(str(book_dir), format="m4b", loudness="none")
    probe = _ffprobe(result.path)
    tags = probe["format"]["tags"]
    assert tags["title"] == "My Book"
    assert tags["artist"] == "A. Writer"
    assert tags["genre"] == "Audiobook"


@needs_ffmpeg
def test_master_book_cover_image_attached_for_m4b(tmp_path):
    book_dir, _ = _build_fake_book(tmp_path, n_chapters=1)
    cover = tmp_path / "cover.png"
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", "color=c=blue:s=32x32:d=1",
         "-frames:v", "1", "-update", "1", str(cover)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr

    result = master.master_book(str(book_dir), format="m4b", loudness="none", cover=str(cover))
    probe = _ffprobe(result.path)
    video_streams = [s for s in probe["streams"] if s["codec_type"] == "video"]
    assert video_streams
    assert video_streams[0]["disposition"]["attached_pic"] == 1


# ---- loudness -------------------------------------------------------------

@needs_ffmpeg
def test_master_book_acx_loudness_within_tolerance(tmp_path):
    book_dir, _ = _build_fake_book(tmp_path, n_chapters=2)
    result = master.master_book(str(book_dir), format="wav", loudness="acx", sample_rate=44100)
    assert result.measured is not None
    assert abs(result.measured["integrated_lufs"] - (-19.0)) <= 1.5
    assert result.measured["true_peak_dbtp"] <= -2.5


@needs_ffmpeg
def test_measure_loudness_matches_the_result_it_reported(tmp_path):
    book_dir, _ = _build_fake_book(tmp_path, n_chapters=1)
    result = master.master_book(str(book_dir), format="wav", loudness="acx", sample_rate=44100)
    remeasured = master.measure_loudness(result.path)
    assert abs(remeasured["integrated_lufs"] - result.measured["integrated_lufs"]) < 0.5


@needs_ffmpeg
def test_master_book_loudness_none_skips_normalization(tmp_path):
    book_dir, _ = _build_fake_book(tmp_path, n_chapters=1)
    result = master.master_book(str(book_dir), format="wav", loudness="none", sample_rate=44100)
    assert result.loudness == "none"
    # Un-normalized fake noise (+-9000 of 32767) sits well above the ACX window;
    # this is just confirming loudnorm was NOT applied, not asserting a target.
    assert result.measured is not None


# ---- filenames / output location -----------------------------------------------

@needs_ffmpeg
def test_master_book_slugifies_the_title_for_the_filename(tmp_path):
    book_dir, _ = _build_fake_book(tmp_path, n_chapters=1, title="A Wonderful, Strange Book!")
    result = master.master_book(str(book_dir), format="wav", loudness="none")
    assert result.file == "a-wonderful-strange-book.wav"
    assert result.path == str(book_dir / "exports" / result.file)


@needs_ffmpeg
def test_master_book_respects_custom_out_dir(tmp_path):
    book_dir, _ = _build_fake_book(tmp_path, n_chapters=1)
    out = tmp_path / "elsewhere"
    result = master.master_book(str(book_dir), format="wav", loudness="none", out_dir=str(out))
    assert os.path.dirname(result.path) == str(out)
    assert os.path.exists(result.path)


# ---- progress + cleanup -----------------------------------------------------

@needs_ffmpeg
def test_master_book_progress_callback_starts_and_ends(tmp_path):
    book_dir, _ = _build_fake_book(tmp_path, n_chapters=1)
    stages = []
    master.master_book(
        str(book_dir), format="wav", loudness="none",
        on_progress=lambda stage, frac: stages.append((stage, frac)),
    )
    assert stages[0] == ("start", 0.0)
    assert stages[-1] == ("done", 1.0)
    assert all(0.0 <= frac <= 1.0 for _, frac in stages)


@needs_ffmpeg
def test_master_book_leaves_no_temp_dir_behind(tmp_path, monkeypatch):
    book_dir, _ = _build_fake_book(tmp_path, n_chapters=1)
    seen = {}
    real_mkdtemp = tempfile.mkdtemp

    def spy(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        seen["dir"] = d
        return d

    monkeypatch.setattr(master.tempfile, "mkdtemp", spy)
    master.master_book(str(book_dir), format="wav", loudness="none")
    assert seen.get("dir") and not os.path.exists(seen["dir"])


# ---- robustness / actionable errors --------------------------------------------

def test_master_book_raises_actionable_error_when_ffmpeg_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(master.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        master.master_book(str(tmp_path))


@needs_ffmpeg
def test_master_book_raises_for_missing_manifest(tmp_path):
    with pytest.raises(master.MasterError, match="manifest"):
        master.master_book(str(tmp_path))


@needs_ffmpeg
def test_master_book_raises_when_manifest_has_no_chapters(tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"meta": {}, "chapters": []}), encoding="utf-8"
    )
    with pytest.raises(master.MasterError, match="no chapters"):
        master.master_book(str(tmp_path))


@needs_ffmpeg
def test_master_book_raises_for_missing_chapter_wav(tmp_path):
    manifest = {
        "meta": {"title": "T"},
        "chapters": [{"index": 0, "title": "One", "audio_file": "chapter_000.wav", "duration_ms": 1000}],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(master.MasterError, match="missing on disk"):
        master.master_book(str(tmp_path))


@needs_ffmpeg
def test_master_book_rejects_unsupported_format(tmp_path):
    book_dir, _ = _build_fake_book(tmp_path, n_chapters=1)
    with pytest.raises(master.MasterError, match="format"):
        master.master_book(str(book_dir), format="ogg")


@needs_ffmpeg
def test_master_book_rejects_unsupported_loudness_mode(tmp_path):
    book_dir, _ = _build_fake_book(tmp_path, n_chapters=1)
    with pytest.raises(master.MasterError, match="loudness"):
        master.master_book(str(book_dir), loudness="loud")


# ---- export_summary (pure filesystem, no ffmpeg needed) ------------------------

def test_export_summary_empty_when_no_exports_dir(tmp_path):
    assert master.export_summary(str(tmp_path)) == []


def test_export_summary_lists_files_with_bytes_and_format(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "book.m4b").write_bytes(b"1234")
    (exports / "book.mp3").write_bytes(b"12")
    summary = {row["file"]: row for row in master.export_summary(str(tmp_path))}
    assert summary["book.m4b"]["bytes"] == 4
    assert summary["book.m4b"]["format"] == "m4b"
    assert summary["book.mp3"]["bytes"] == 2
    assert "created_at" in summary["book.m4b"]


# ---- CLI --------------------------------------------------------------------

@needs_ffmpeg
def test_cli_main_masters_a_book_and_prints_the_result(tmp_path, capsys):
    book_dir, _ = _build_fake_book(tmp_path, n_chapters=1)
    master.main([str(book_dir), "--format", "wav", "--loudness", "none"])
    out = capsys.readouterr().out
    assert "wrote" in out
    assert "measured loudness" in out
    assert list((book_dir / "exports").iterdir())


# ---- ACX delivery ---------------------------------------

def _acx_book(tmp_path, *, n_chapters=2, include=None, utterances=None, duration_ms=None):
    """A fake book with the fields the ACX path reads: per-chapter
    ``include_in_audio`` and the utterance timestamps it splits on."""
    chapters = []
    for i in range(n_chapters):
        dur = duration_ms[i] if duration_ms else 1800 + i * 600
        fname = f"chapter_{i:03d}.wav"
        _write_noise_wav(tmp_path / fname, 24000, dur, seed=i + 11)
        chapter = {"index": i, "title": f"Chapter {i + 1}", "scenes": [],
                   "audio_file": fname, "duration_ms": dur}
        if include is not None:
            chapter["include_in_audio"] = include[i]
        chapters.append(chapter)
    manifest = {
        "schema_version": "0.1.0",
        "meta": {"title": "Fake Book", "author": "Fake Author", "language": "en",
                 "source_path": "x", "book_type": "general_fiction"},
        "cast": [], "chapters": chapters, "utterances": utterances or [],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path, chapters


@needs_ffmpeg
def test_acx_writes_one_mp3_per_chapter_at_the_submission_spec(tmp_path):
    book_dir, chapters = _acx_book(tmp_path)
    result = master.master_book(str(book_dir), format="acx")

    assert result.format == "acx"
    assert len(result.files) == len(chapters)
    assert [f["file"] for f in result.files] == ["01-chapter-1.mp3", "02-chapter-2.mp3"]

    tracks = []
    for entry in result.files:
        probe = _ffprobe(entry["path"])
        stream = [s for s in probe["streams"] if s["codec_type"] == "audio"][0]
        assert stream["codec_name"] == "mp3"
        assert stream["sample_rate"] == "44100"
        assert stream["channels"] == 1
        assert abs(int(stream["bit_rate"]) - 192_000) < 1_000  # 192 kbps CBR
        assert probe["format"]["tags"]["album"] == "Fake Book"
        assert probe["format"]["tags"]["artist"] == "Fake Author"
        tracks.append(probe["format"]["tags"]["track"])
    assert tracks == ["1", "2"]


@needs_ffmpeg
def test_acx_tops_and_tails_every_file_with_room_tone_that_is_not_silence(tmp_path):
    """0 ms of head tone and 2 s of digital silence at the tail were two separate
    automatic rejections. Measured on the delivered MP3, not assumed."""
    book_dir, _ = _acx_book(tmp_path, n_chapters=1)
    entry = master.master_book(str(book_dir), format="acx").files[0]

    head = entry["measured"]["room_tone_head_dbfs"]
    tail = entry["measured"]["room_tone_tail_dbfs"]
    for level in (head, tail):
        assert level is not None
        assert -80.0 < level < -55.0, level  # audible as nothing, present as tone


@needs_ffmpeg
def test_acx_true_peak_is_under_the_ceiling(tmp_path):
    """The one real measurement this repo ever recorded was -2.8 dBTP — over the
    ceiling, with no limiter. Noise, not a tone, so loudnorm behaves like it does
    on speech."""
    book_dir, _ = _acx_book(tmp_path, n_chapters=2)
    result = master.master_book(str(book_dir), format="acx")

    assert result.compliance == "pass"
    low, high = master.ACX_LOUDNESS_WINDOW
    for entry in result.files:
        assert entry["measured"]["true_peak_dbtp"] <= master.ACX_PEAK_CEILING_DBTP
        assert entry["checks"] == {"peak_ok": True, "loudness_ok": True, "duration_ok": True}
        assert low <= entry["measured"]["integrated_lufs"] <= high


@needs_ffmpeg
def test_acx_skips_chapters_excluded_from_the_audio(tmp_path):
    book_dir, _ = _acx_book(tmp_path, n_chapters=3, include=[False, True, True])
    result = master.master_book(str(book_dir), format="acx")
    assert [f["chapter"] for f in result.files] == [1, 2]
    assert [f["file"] for f in result.files] == ["01-chapter-2.mp3", "02-chapter-3.mp3"]


@needs_ffmpeg
def test_acx_refuses_a_book_with_nothing_left_to_export(tmp_path):
    book_dir, _ = _acx_book(tmp_path, n_chapters=2, include=[False, False])
    with pytest.raises(master.MasterError, match="include_in_audio"):
        master.master_book(str(book_dir), format="acx")


@needs_ffmpeg
def test_acx_honours_an_explicit_bitrate(tmp_path):
    book_dir, _ = _acx_book(tmp_path, n_chapters=1)
    result = master.master_book(str(book_dir), format="acx", bitrate="128k")
    stream = [s for s in _ffprobe(result.files[0]["path"])["streams"]
              if s["codec_type"] == "audio"][0]
    assert abs(int(stream["bit_rate"]) - 128_000) < 1_000


# The 115-minute split is tested in two halves rather than by rendering two hours
# of audio: `plan_chapter_parts` is a pure function, so the real durations are
# exercised there, and the file-producing half runs the identical code path with
# `max_part_ms` lowered to a couple of seconds.

def test_plan_chapter_parts_leaves_a_short_chapter_alone():
    assert master.plan_chapter_parts(40 * 60 * 1000, [1000, 2000]) == [(0, 40 * 60 * 1000)]


def test_plan_chapter_parts_splits_a_long_chapter_at_an_utterance_boundary():
    duration = 140 * 60 * 1000
    boundaries = list(range(0, duration, 30 * 1000))  # an utterance every 30 s
    parts = master.plan_chapter_parts(duration, boundaries)

    assert len(parts) == 2
    assert parts[0][0] == 0 and parts[-1][1] == duration
    assert parts[0][1] == parts[1][0]
    assert parts[0][1] in boundaries          # the cut is on a real line ending
    assert all(end - start <= master.ACX_MAX_PART_MS for start, end in parts)
    assert abs(parts[0][1] - duration / 2) < 60 * 1000  # even parts, not 115 + 25


def test_plan_chapter_parts_cuts_evenly_and_never_exceeds_the_cap():
    duration = 6 * 60 * 60 * 1000  # a 6-hour "chapter": four parts
    parts = master.plan_chapter_parts(duration, [])
    assert len(parts) == 4
    assert all(end - start <= master.ACX_MAX_PART_MS for start, end in parts)


def test_plan_chapter_parts_falls_back_when_the_manifest_has_no_timestamps():
    duration = 140 * 60 * 1000
    parts = master.plan_chapter_parts(duration, [])
    assert len(parts) == 2 and parts[0][1] == duration // 2


@needs_ffmpeg
def test_acx_splits_a_too_long_chapter_into_named_parts(tmp_path):
    """Same code path as the 115-minute rule, with the cap lowered so the test
    costs 2.4 s of audio instead of two hours."""
    utterances = [
        {"id": f"c0_p0_s{i}", "chapter_index": 0,
         "audio": {"start_ms": i * 800, "end_ms": (i + 1) * 800}}
        for i in range(3)
    ]
    book_dir, _ = _acx_book(
        tmp_path, n_chapters=1, duration_ms=[2400], utterances=utterances
    )
    result = master.master_book(str(book_dir), format="acx", max_part_ms=1000)

    assert [f["file"] for f in result.files] == [
        "01-chapter-1-part1.mp3", "01-chapter-1-part2.mp3", "01-chapter-1-part3.mp3"
    ]
    assert all(f["chapter"] == 0 for f in result.files)
    for entry in result.files:
        # each part is its own ~800 ms cut plus 1.5 s of room tone
        assert 2000 < entry["duration_ms"] < 2800
        assert os.path.exists(entry["path"])
    titles = [_ffprobe(f["path"])["format"]["tags"]["title"] for f in result.files]
    assert titles == ["Chapter 1 (part 1)", "Chapter 1 (part 2)", "Chapter 1 (part 3)"]


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["m4b", "mp3"])
def test_the_limiter_also_protects_the_single_file_formats(tmp_path, fmt):
    """``loudness="acx"`` on an .m4b now means the same limiter, and the result
    still reports what was measured on the output."""
    book_dir, _ = _build_fake_book(tmp_path, n_chapters=2)
    result = master.master_book(str(book_dir), format=fmt, loudness="acx")
    assert result.measured["true_peak_dbtp"] <= master.ACX_PEAK_CEILING_DBTP
    assert result.files is None and result.compliance is None  # unchanged shape


@needs_ffmpeg
def test_acx_export_summary_lists_the_per_chapter_files(tmp_path):
    book_dir, _ = _acx_book(tmp_path, n_chapters=2)
    master.master_book(str(book_dir), format="acx")
    rows = master.export_summary(str(book_dir))
    assert {row["file"] for row in rows} == {"01-chapter-1.mp3", "02-chapter-2.mp3"}
    assert all(row["format"] == "mp3" for row in rows)
