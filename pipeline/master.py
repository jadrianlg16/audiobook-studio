"""Mastering + export: a rendered book folder (chapter_NNN.wav + manifest.json)
-> a single chaptered audiobook file (.m4b / .mp3 / .wav), **or** an ACX
submission pack (``format="acx"``): one 192 kbps CBR MP3 per chapter, room tone
at both ends, true-peak limited, split before 120 minutes, and measured.

Pure stdlib + ``subprocess`` calling ``ffmpeg``/``ffprobe`` — no pydub, no other
third-party audio library.

The difference between the two is not cosmetic. ACX takes per-chapter files, and
``.m4b`` is a consumer container it does not accept at all; a whole-book m4b at
96 kbps with no head room tone and no limiter is rejected on upload without a
human ever hearing it. ``format="acx"`` is therefore a delivery *shape*, and it
reports a compliance table (measured loudness, true peak, room-tone level and
duration, per file) so the answer is known before the upload rather than after.

ACX loudness (the Audiobook Creation Exchange submission spec, approximated
here): RMS loudness between -23 dBFS and -18 dBFS, with peaks no louder than
-3 dBTP. EBU R128 integrated loudness (LUFS) is the closest widely-available
approximation of that RMS window, so ``loudness="acx"`` runs a two-pass
``loudnorm`` (first pass measures the concatenated audio; second pass applies a
*linear* gain using the measured stats, which is ffmpeg's documented
recipe for hitting an exact target rather than the single-pass filter's dynamic
compression) targeting ``I=-19:TP=-3:LRA=11`` — comfortably inside the ACX
window with headroom for the container's own encoding.

Sample rate: ACX (and most audiobook validators) require the final file at
44.1 kHz even though the synthesized voice itself is band-limited around the
engine's native rate (commonly 24 kHz for Kokoro). Resampling up does not add
information, it only satisfies the container/validator requirement.

Known container limitation (verified empirically, not just assumed): the WAV
muxer accepts ``-map_chapters``/``-map_metadata`` without error, and tags come
through, but it silently drops chapter markers — WAV has no chapter mechanism.
``.m4b`` (MP4) and ``.mp3`` (ID3v2) both round-trip chapters and a cover image
correctly (confirmed with ``ffprobe -show_chapters`` on real output).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

# ACX-ish EBU R128 target. TP is intentionally 3 dB below the ACX ceiling of
# -3 dBTP is the ceiling itself; loudnorm's linear two-pass mode uses this as
# the value to normalize *toward*, not a hard limiter, so real output peaks can
# land a bit under this depending on program dynamics.
_LOUDNORM_TARGET = {"I": -19.0, "TP": -3.0, "LRA": 11.0}

# ---- ACX submission profile ---------------------------------------------------
# What ACX actually rejects an upload for, as opposed to what merely sounds bad.
# Every number here is a submission requirement, not a taste call.
ACX_BITRATE = "192k"          # 192 kbps CBR MP3, one file per chapter
ACX_SAMPLE_RATE = 44100
ACX_CHANNELS = 1
ACX_ROOM_TONE_MS = 750        # 0.5-1.0 s of room tone at head AND tail
ACX_MAX_PART_MS = 115 * 60 * 1000   # split before the 120-minute hard ceiling
ACX_MAX_DURATION_MS = 120 * 60 * 1000
ACX_PEAK_CEILING_DBTP = -3.0
ACX_LOUDNESS_WINDOW = (-23.0, -18.0)  # the RMS window, measured here as LUFS
_ACX_LIMITER = "alimiter=limit=0.67:level=disabled"
"""True-peak limiter after loudnorm. 0.67 ≈ -3.5 dBTP, half a dB under the ACX
ceiling so the MP3 encoder's own overshoot still lands inside it. ``loudnorm``
*targets* a true peak, it does not enforce one — the one real measurement this
repo ever recorded was -2.8 dBTP, i.e. a rejection, with the note "no limiter
yet". ``level=disabled`` is load-bearing: alimiter's auto-level would helpfully
push the whole programme back up to the ceiling and undo the loudness pass."""

# Room tone, not digital silence. ACX rejects a file that opens on absolute
# zero — and a listener hears the cut. Pink noise at this amplitude measures
# around -70 dBFS RMS: inaudible, comfortably under the -60 dB noise-floor
# requirement, and unmistakably not \x00.
_ACX_ROOM_TONE_AMPLITUDE = 0.0012
_ACX_ROOM_TONE_COLOR = "pink"

_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}")
_VOLUMEDETECT_MEAN_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")

OnProgress = Callable[[str, float], None]


class MasterError(RuntimeError):
    """Actionable mastering failure: missing ffmpeg, missing chapter audio,
    an empty manifest, or ffmpeg itself failing. Always includes what to do
    about it or ffmpeg's own error tail."""


@dataclass
class ExportResult:
    file: str
    path: str
    bytes: int
    format: str
    loudness: str
    sample_rate: int
    duration_ms: int
    chapters: int
    measured: Optional[dict] = None
    files: Optional[list[dict]] = None
    """Per-file detail, set only by ``format="acx"`` (which writes one MP3 per
    chapter instead of one book file). Each entry is ``{file, path, bytes,
    chapter, title, duration_ms, measured, checks}``; the studio adds the ``url``
    it serves the file at, since this module knows nothing about HTTP. ``file``/
    ``path``/``bytes`` above point at the first file so every existing caller
    keeps working."""
    compliance: Optional[str] = None
    """``"pass"`` when every check on every file passed, else ``"fail"``. The
    point of the whole feature: see green *before* the upload, not after."""


# ---- ffmpeg availability -----------------------------------------------------

def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def ffmpeg_version() -> Optional[str]:
    if shutil.which("ffmpeg") is None:
        return None
    try:
        proc = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=10, check=False
        )
    except OSError:
        return None
    first_line = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
    m = re.search(r"ffmpeg version (\S+)", first_line)
    return m.group(1) if m else (first_line.strip() or None)


def _require_ffmpeg() -> None:
    if not ffmpeg_available():
        raise MasterError(
            "ffmpeg/ffprobe not found on PATH. Install ffmpeg (e.g. `apt-get install "
            "ffmpeg` on Debian/Ubuntu, or download a build from https://ffmpeg.org/) "
            "and make sure both `ffmpeg` and `ffprobe` are runnable before mastering "
            "a book."
        )


# ---- loudness measurement -----------------------------------------------------

def _parse_loudnorm_json(stderr: str) -> Optional[dict]:
    """loudnorm's ``print_format=json`` writes one flat JSON object (values are
    themselves JSON strings, e.g. ``"input_i": "-13.07"``) near the end of
    stderr. Take the last brace-delimited block — loudnorm never nests braces."""
    matches = _JSON_BLOCK_RE.findall(stderr or "")
    for candidate in reversed(matches):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if "input_i" in data:
            return data
    return None


def measure_loudness(path: str) -> dict:
    """Integrated loudness / true peak / loudness range of ``path``, via a
    single ``loudnorm`` analysis pass to a null output (nothing is written)."""
    _require_ffmpeg()
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-i", path,
        "-af", "loudnorm=print_format=json", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    data = _parse_loudnorm_json(proc.stderr)
    if data is None:
        raise MasterError(
            f"could not measure loudness of {path!r}: ffmpeg produced no loudnorm "
            f"JSON (stderr tail: {proc.stderr[-500:]!r})"
        )
    return {
        "integrated_lufs": float(data["input_i"]),
        "true_peak_dbtp": float(data["input_tp"]),
        "lra": float(data["input_lra"]),
    }


# ---- small ffmpeg-invoking helpers -------------------------------------------

def _run_ffmpeg(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MasterError(f"ffmpeg {what} failed:\n{proc.stderr[-2000:]}")
    return proc


def _concat_wavs(paths: list[str], dest: str, tmp_dir: str) -> None:
    """Concatenate chapter WAVs in order via the concat demuxer. Re-encodes to
    pcm_s16le (cheap, since the inputs are already 16-bit PCM) rather than
    `-c copy`, so a stray format mismatch between chapters fails loudly here
    instead of producing a subtly broken concat."""
    list_path = os.path.join(tmp_dir, "concat_list.txt")
    with open(list_path, "w", encoding="utf-8") as fh:
        for p in paths:
            escaped = os.path.abspath(p).replace("\\", "/").replace("'", "'\\''")
            fh.write(f"file '{escaped}'\n")
    _run_ffmpeg(
        ["ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
         "-c:a", "pcm_s16le", dest],
        "concat",
    )


def _loudnorm_two_pass(
    src: str, dest: str, sample_rate: int, *, limiter: bool = True, channels: int = 0
) -> dict:
    """Two-pass EBU R128 loudnorm targeting the ACX-ish window, then resample
    to ``sample_rate`` in the same filter chain as the second pass (steps 2+3
    of the module contract collapse into one ffmpeg invocation).

    ``limiter`` appends a true-peak limiter after the normalization — loudnorm
    aims at ``TP`` but does not enforce it, which is how this repo's one real
    measurement came out 0.2 dB over the ACX ceiling.
    """
    t = _LOUDNORM_TARGET
    pass1_filter = f"loudnorm=I={t['I']}:TP={t['TP']}:LRA={t['LRA']}:print_format=json"
    proc1 = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-i", src, "-af", pass1_filter, "-f", "null", "-"],
        capture_output=True, text=True,
    )
    measured = _parse_loudnorm_json(proc1.stderr)
    if measured is None:
        raise MasterError(
            "loudnorm analysis pass produced no measurement — is the concatenated "
            f"audio silent or too short? (stderr tail: {proc1.stderr[-500:]!r})"
        )

    pass2_filter = (
        f"loudnorm=I={t['I']}:TP={t['TP']}:LRA={t['LRA']}:"
        f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
        f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:linear=true:print_format=summary,"
        f"aresample={sample_rate}"
    )
    if limiter:
        pass2_filter += f",{_ACX_LIMITER}"
    cmd = ["ffmpeg", "-nostdin", "-y", "-i", src, "-af", pass2_filter,
           "-ar", str(sample_rate)]
    if channels:
        cmd += ["-ac", str(channels)]
    _run_ffmpeg(cmd + ["-c:a", "pcm_s16le", dest], "loudnorm second pass")
    return {
        "integrated_lufs": float(measured["input_i"]),
        "true_peak_dbtp": float(measured["input_tp"]),
        "lra": float(measured["input_lra"]),
    }


def _resample(src: str, dest: str, sample_rate: int) -> None:
    _run_ffmpeg(
        ["ffmpeg", "-nostdin", "-y", "-i", src, "-af", f"aresample={sample_rate}",
         "-ar", str(sample_rate), "-c:a", "pcm_s16le", dest],
        "resample",
    )


def _escape_ffmeta(value: str) -> str:
    # ffmetadata escapes '=', ';', '#', '\' and literal newlines with a backslash.
    return re.sub(r"([=;#\\\n])", r"\\\1", value)


def _write_ffmetadata(
    path: str, titles: list[str], durations_ms: list[int], *, title: str, author: str
) -> None:
    lines = [";FFMETADATA1", f"title={_escape_ffmeta(title)}"]
    if author:
        lines.append(f"artist={_escape_ffmeta(author)}")
    lines.append(f"album={_escape_ffmeta(title)}")
    lines.append("genre=Audiobook")
    cursor = 0
    for chapter_title, dur in zip(titles, durations_ms):
        start = cursor
        end = cursor + max(int(dur), 1)  # a zero-length chapter is invalid ffmetadata
        lines += [
            "",
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start}",
            f"END={end}",
            f"title={_escape_ffmeta(chapter_title)}",
        ]
        cursor = end
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _encode_final(
    src: str,
    chapmeta_path: str,
    dest: str,
    *,
    format: str,
    sample_rate: int,
    bitrate: str,
    cover: Optional[str],
) -> None:
    # Chapters via an ffmetadata "input": ffmpeg auto-detects the demuxer for a
    # plain-text ;FFMETADATA1 file, and -map_chapters/-map_metadata copy its
    # chapters + global tags onto the output. Verified against ffprobe on both
    # m4b and mp3; WAV accepts the mapping but the container drops chapters.
    allow_cover = format in ("m4b", "mp3") and bool(cover) and os.path.exists(cover)

    cmd = ["ffmpeg", "-nostdin", "-y", "-i", src, "-i", chapmeta_path]
    if allow_cover:
        cmd += ["-i", cover]

    cmd += ["-map_metadata", "1", "-map_chapters", "1", "-map", "0:a"]
    if allow_cover:
        cmd += ["-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]

    if format == "m4b":
        cmd += ["-c:a", "aac", "-b:a", bitrate, "-ar", str(sample_rate),
                "-movflags", "+faststart", "-f", "mp4"]
    elif format == "mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", bitrate, "-ar", str(sample_rate)]
    elif format == "wav":
        cmd += ["-c:a", "pcm_s16le", "-ar", str(sample_rate)]
    else:
        raise MasterError(f"unsupported format: {format!r} (expected m4b, mp3, or wav)")

    cmd.append(dest)
    _run_ffmpeg(cmd, f"encode ({format})")


def _probe_duration_ms(path: str) -> Optional[int]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", path],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        return round(float(data["format"]["duration"]) * 1000)
    except (KeyError, ValueError, json.JSONDecodeError):
        return None


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s or "audiobook"


# ---- ACX: per-chapter delivery ------------------------------------------------

def plan_chapter_parts(
    duration_ms: int, boundaries_ms: Sequence[int], max_part_ms: int = ACX_MAX_PART_MS
) -> list[tuple[int, int]]:
    """Split one chapter into ``[(start_ms, end_ms), …]`` no longer than the cap.

    ACX refuses a file over 120 minutes, so a long chapter has to be delivered in
    parts — and a part must never begin mid-sentence. ``boundaries_ms`` are the
    utterance end times from the manifest; each cut lands on the nearest one, so
    part 2 opens on a new line the way a listener expects.

    Parts are cut evenly (``ceil(duration / cap)`` of them) rather than "115
    minutes then the remainder", which would otherwise hand ACX a 4-minute
    part 2 for a 119-minute chapter.
    """
    if duration_ms <= max_part_ms or max_part_ms <= 0:
        return [(0, duration_ms)]

    count = -(-duration_ms // max_part_ms)  # ceil
    usable = sorted({int(b) for b in boundaries_ms if 0 < int(b) < duration_ms})
    cuts: list[int] = []
    for k in range(1, count):
        target = round(duration_ms * k / count)
        if usable:
            target = min(usable, key=lambda b: (abs(b - target), b))
        if cuts and target <= cuts[-1]:
            continue  # two targets snapped to the same boundary; keep one part
        cuts.append(target)

    edges = [0, *cuts, duration_ms]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def _chapter_boundaries(manifest: dict, chapter_index: int) -> list[int]:
    """Utterance end times inside one chapter, from the rendered manifest."""
    out: list[int] = []
    for u in manifest.get("utterances") or []:
        if u.get("chapter_index") != chapter_index:
            continue
        audio = u.get("audio") or {}
        end = audio.get("end_ms")
        if isinstance(end, (int, float)):
            out.append(int(end))
    return sorted(out)


def _write_room_tone(dest: str, sample_rate: int, duration_ms: int) -> None:
    """Generate room tone: very low pink noise, not ``\\x00``.

    Digital silence at the head of a file is one of the things ACX rejects for,
    and it is audible as a hole before the first word. Real production tops and
    tails with room tone recorded in the booth; there is no booth here, so the
    tone is synthesized at a level below anything a listener can hear.
    """
    _run_ffmpeg(
        ["ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i",
         f"anoisesrc=color={_ACX_ROOM_TONE_COLOR}:sample_rate={sample_rate}"
         f":amplitude={_ACX_ROOM_TONE_AMPLITUDE}:duration={duration_ms / 1000}:seed=7",
         "-ac", str(ACX_CHANNELS), "-c:a", "pcm_s16le", dest],
        "room tone",
    )


def _segment_rms_dbfs(path: str, *, from_end: bool, seconds: float = 0.5) -> Optional[float]:
    """Mean (RMS) level of the first or last ``seconds`` of ``path``, in dBFS.

    This is how the room tone is *checked* rather than assumed: a head that
    measures -inf is digital silence, and one that measures -30 is not room tone,
    it is programme.
    """
    seek = ["-sseof", f"-{seconds}"] if from_end else ["-ss", "0"]
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", *seek, "-t", str(seconds), "-i", path,
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = None
    for match in _VOLUMEDETECT_MEAN_RE.finditer(proc.stderr or ""):
        pass
    return float(match.group(1)) if match else None


def _encode_acx_part(
    body_path: str, tone_path: str, dest: str, tmp_dir: str, *,
    bitrate: str, sample_rate: int, tags: dict,
) -> None:
    """Room tone + body + room tone → one CBR MP3 with ID3 tags."""
    assembled = os.path.join(tmp_dir, "assembled.wav")
    _concat_wavs([tone_path, body_path, tone_path], assembled, tmp_dir)

    cmd = ["ffmpeg", "-nostdin", "-y", "-i", assembled,
           "-c:a", "libmp3lame",
           # CBR, stated three ways: libmp3lame will happily drift to VBR-ish
           # frames otherwise, and "192 kbps CBR" is a submission requirement.
           "-b:a", bitrate, "-minrate", bitrate, "-maxrate", bitrate,
           "-bufsize", bitrate, "-abr", "0",
           "-ar", str(sample_rate), "-ac", str(ACX_CHANNELS),
           "-write_xing", "0", "-id3v2_version", "3"]
    for key, value in tags.items():
        if value:
            cmd += ["-metadata", f"{key}={value}"]
    _run_ffmpeg(cmd + [dest], "encode (acx mp3)")


def _acx_checks(measured: dict, duration_ms: int) -> dict:
    low, high = ACX_LOUDNESS_WINDOW
    return {
        "peak_ok": measured["true_peak_dbtp"] <= ACX_PEAK_CEILING_DBTP,
        "loudness_ok": low <= measured["integrated_lufs"] <= high,
        "duration_ok": duration_ms <= ACX_MAX_DURATION_MS,
    }


def _master_acx(
    book_dir: str,
    manifest: dict,
    chapters: list[dict],
    dest_dir: str,
    tmp_dir: str,
    *,
    sample_rate: int,
    bitrate: str,
    title: str,
    author: str,
    max_part_ms: int,
    progress: Callable[[str, float], None],
) -> ExportResult:
    """One 192 kbps CBR MP3 per included chapter, measured and checked.

    Per file: extract → two-pass loudnorm → true-peak limiter → 44.1 kHz mono →
    0.75 s of room tone at each end → CBR MP3 with ID3 tags → measure the result
    and check it against the submission spec. Nothing is asserted about the
    output that was not measured on the output.
    """
    tone_path = os.path.join(tmp_dir, "room_tone.wav")
    _write_room_tone(tone_path, sample_rate, ACX_ROOM_TONE_MS)

    files: list[dict] = []
    total_duration_ms = 0
    total_units = sum(
        len(plan_chapter_parts(
            int(ch.get("duration_ms") or 0),
            _chapter_boundaries(manifest, int(ch.get("index", 0))),
            max_part_ms,
        ))
        for ch in chapters
    ) or 1
    done = 0

    for position, ch in enumerate(chapters, start=1):
        index = int(ch.get("index", position - 1))
        chapter_title = ch.get("title") or f"Chapter {position}"
        source = os.path.join(book_dir, ch["audio_file"])
        duration_ms = int(ch.get("duration_ms") or 0) or (_probe_duration_ms(source) or 0)
        parts = plan_chapter_parts(
            duration_ms, _chapter_boundaries(manifest, index), max_part_ms
        )

        for part_number, (start_ms, end_ms) in enumerate(parts, start=1):
            stem = f"{position:02d}-{_slugify(chapter_title)}"
            if len(parts) > 1:
                stem = f"{stem}-part{part_number}"
            name = f"{stem}.mp3"
            final_path = os.path.join(dest_dir, name)

            cut = os.path.join(tmp_dir, "cut.wav")
            cut_cmd = ["ffmpeg", "-nostdin", "-y"]
            if start_ms:
                cut_cmd += ["-ss", f"{start_ms / 1000:.3f}"]
            if len(parts) > 1:
                cut_cmd += ["-to", f"{end_ms / 1000:.3f}"]
            _run_ffmpeg(
                cut_cmd + ["-i", source, "-ac", str(ACX_CHANNELS),
                           "-c:a", "pcm_s16le", cut],
                f"extract {name}",
            )

            body = os.path.join(tmp_dir, "body.wav")
            _loudnorm_two_pass(cut, body, sample_rate, limiter=True, channels=ACX_CHANNELS)
            _encode_acx_part(
                body, tone_path, final_path, tmp_dir,
                bitrate=bitrate, sample_rate=sample_rate,
                tags={
                    "title": chapter_title if len(parts) == 1
                             else f"{chapter_title} (part {part_number})",
                    "artist": author,
                    "album": title,
                    "album_artist": author,
                    "track": str(position),
                    "genre": "Audiobook",
                },
            )

            measured = measure_loudness(final_path)
            measured["room_tone_head_dbfs"] = _segment_rms_dbfs(final_path, from_end=False)
            measured["room_tone_tail_dbfs"] = _segment_rms_dbfs(final_path, from_end=True)
            file_duration_ms = _probe_duration_ms(final_path) or (
                end_ms - start_ms + 2 * ACX_ROOM_TONE_MS
            )
            total_duration_ms += file_duration_ms
            files.append({
                "file": name,
                "path": final_path,
                "bytes": os.path.getsize(final_path),
                "chapter": index,
                "title": chapter_title,
                "part": part_number,
                "parts": len(parts),
                "duration_ms": file_duration_ms,
                "measured": measured,
                "checks": _acx_checks(measured, file_duration_ms),
            })
            done += 1
            progress(f"acx {name}", 0.2 + 0.75 * done / total_units)

    compliance = "pass" if all(all(f["checks"].values()) for f in files) else "fail"
    first = files[0]
    return ExportResult(
        file=first["file"],
        path=first["path"],
        bytes=sum(f["bytes"] for f in files),
        format="acx",
        loudness="acx",
        sample_rate=sample_rate,
        duration_ms=total_duration_ms,
        chapters=len(chapters),
        measured=first["measured"],
        files=files,
        compliance=compliance,
    )


# ---- the public entry point ---------------------------------------------------

def master_book(
    book_dir: str,
    *,
    out_dir: Optional[str] = None,
    format: str = "m4b",
    loudness: str = "acx",
    sample_rate: int = 44100,
    bitrate: Optional[str] = None,
    title: Optional[str] = None,
    author: Optional[str] = None,
    cover: Optional[str] = None,
    on_progress: Optional[OnProgress] = None,
    max_part_ms: int = ACX_MAX_PART_MS,
) -> ExportResult:
    """Master a rendered book folder (``manifest.json`` + ``chapter_NNN.wav``)
    into one chaptered audiobook file at ``book_dir/exports/<slug>.<ext>`` (or
    under ``out_dir`` if given).

    Steps: (1) concat the chapter WAVs in order, (2-3) loudness-normalize
    (``loudness="acx"``) and/or resample to ``sample_rate``, (4) encode to the
    target format, (5) mux in chapter markers + tags (+ cover, m4b/mp3 only).
    Returns loudness measured on the *output* file so the caller can show it.

    ``format="acx"`` is a different shape of delivery, not another container:
    **one 192 kbps CBR MP3 per chapter** (skipping chapters whose
    ``include_in_audio`` is false), each topped and tailed with 0.75 s of room
    tone, loudness-normalized, true-peak limited, split if it runs past 115
    minutes, and **measured**. The result carries ``files[]`` (with ``measured``
    and ``checks`` per file) and an overall ``compliance``, so a producer can see
    green before the upload instead of finding out from ACX afterwards.
    """
    _require_ffmpeg()
    if format not in ("m4b", "mp3", "wav", "acx"):
        raise MasterError(
            f"unsupported format: {format!r} (expected m4b, mp3, wav, or acx)"
        )
    if loudness not in ("acx", "none"):
        raise MasterError(f"unsupported loudness mode: {loudness!r} (expected acx or none)")
    # An explicit bitrate is always honoured; the default differs by format,
    # because 96k is a reasonable .m4b and an automatic ACX rejection.
    bitrate = bitrate or (ACX_BITRATE if format == "acx" else "96k")

    manifest_path = os.path.join(book_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        raise MasterError(f"no manifest.json in {book_dir!r} — render the book first")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    chapters = manifest.get("chapters") or []
    if not chapters:
        raise MasterError(f"manifest.json in {book_dir!r} has no chapters to master")

    if format == "acx":
        # A copyright page or a table of contents is not a chapter of the
        # audiobook. Absent (older manifest) means included, so nothing that
        # already renders starts disappearing from an export.
        chapters = [ch for ch in chapters if ch.get("include_in_audio", True)]
        if not chapters:
            raise MasterError(
                f"every chapter in {book_dir!r} is marked include_in_audio=false — "
                f"nothing to export"
            )

    meta = manifest.get("meta") or {}
    book_title = title or meta.get("title") or "audiobook"
    book_author = author if author is not None else meta.get("author") or ""

    chapter_paths: list[str] = []
    chapter_titles: list[str] = []
    chapter_durations_ms: list[int] = []
    for idx, ch in enumerate(chapters):
        audio_file = ch.get("audio_file")
        chapter_title = ch.get("title") or f"Chapter {idx + 1}"
        if not audio_file:
            raise MasterError(
                f"chapter {idx} ({chapter_title!r}) has no audio_file — render it first"
            )
        path = os.path.join(book_dir, audio_file)
        if not os.path.exists(path):
            raise MasterError(f"chapter WAV missing on disk: {path!r}")
        chapter_paths.append(path)
        chapter_titles.append(chapter_title)
        chapter_durations_ms.append(int(ch.get("duration_ms") or 0))

    def progress(stage: str, fraction: float) -> None:
        if on_progress:
            on_progress(stage, fraction)

    progress("start", 0.0)
    tmp_dir = tempfile.mkdtemp(prefix="master_")
    try:
        dest_dir = out_dir or os.path.join(book_dir, "exports")
        os.makedirs(dest_dir, exist_ok=True)

        if format == "acx":
            result = _master_acx(
                book_dir, manifest, chapters, dest_dir, tmp_dir,
                sample_rate=sample_rate,
                bitrate=bitrate,
                title=book_title, author=book_author,
                max_part_ms=max_part_ms, progress=progress,
            )
            progress("done", 1.0)
            return result

        concat_path = os.path.join(tmp_dir, "concat.wav")
        _concat_wavs(chapter_paths, concat_path, tmp_dir)
        progress("concat", 0.2)

        processed_path = os.path.join(tmp_dir, "processed.wav")
        if loudness == "acx":
            _loudnorm_two_pass(concat_path, processed_path, sample_rate)
            progress("loudness", 0.55)
        else:
            _resample(concat_path, processed_path, sample_rate)
            progress("resample", 0.55)

        slug = _slugify(book_title)
        ext = format
        final_name = f"{slug}.{ext}"
        dest_dir = out_dir or os.path.join(book_dir, "exports")
        os.makedirs(dest_dir, exist_ok=True)
        final_path = os.path.join(dest_dir, final_name)

        chapmeta_path = os.path.join(tmp_dir, "chapters.ffmeta")
        _write_ffmetadata(
            chapmeta_path, chapter_titles, chapter_durations_ms,
            title=book_title, author=book_author,
        )

        _encode_final(
            processed_path, chapmeta_path, final_path,
            format=format, sample_rate=sample_rate, bitrate=bitrate, cover=cover,
        )
        progress("encode", 0.9)

        measured = measure_loudness(final_path)
        duration_ms = _probe_duration_ms(final_path)
        if duration_ms is None:
            duration_ms = sum(chapter_durations_ms)

        progress("done", 1.0)
        return ExportResult(
            file=final_name,
            path=final_path,
            bytes=os.path.getsize(final_path),
            format=format,
            loudness=loudness,
            sample_rate=sample_rate,
            duration_ms=duration_ms,
            chapters=len(chapters),
            measured=measured,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def export_summary(book_dir: str) -> list[dict]:
    """List ``book_dir/exports/*`` as ``{file, bytes, created_at, format}`` —
    what the studio serves at ``GET /books/{slug}/exports``."""
    exports_dir = os.path.join(book_dir, "exports")
    if not os.path.isdir(exports_dir):
        return []
    out = []
    for name in sorted(os.listdir(exports_dir)):
        path = os.path.join(exports_dir, name)
        if not os.path.isfile(path):
            continue
        stat = os.stat(path)
        out.append({
            "file": name,
            "bytes": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z"),
            "format": os.path.splitext(name)[1].lstrip(".").lower(),
        })
    return out


# ---- CLI -----------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(
        description="Master a rendered book folder into one chaptered audiobook file."
    )
    ap.add_argument("book_dir", help="a folder with manifest.json + chapter_NNN.wav")
    ap.add_argument("--format", default="m4b", choices=["m4b", "mp3", "wav", "acx"])
    ap.add_argument("--loudness", default="acx", choices=["acx", "none"])
    ap.add_argument("--sample-rate", type=int, default=44100)
    ap.add_argument("--bitrate", default=None,
                    help="default: 192k for --format acx, 96k otherwise")
    ap.add_argument("--out", default=None, help="output directory (default: <book_dir>/exports)")
    ap.add_argument("--title", default=None)
    ap.add_argument("--author", default=None)
    ap.add_argument("--cover", default=None, help="path to a cover image (m4b/mp3 only)")
    args = ap.parse_args(argv)

    def on_progress(stage: str, fraction: float) -> None:
        print(f"  [{fraction * 100:5.1f}%] {stage}", flush=True)

    result = master_book(
        args.book_dir,
        out_dir=args.out,
        format=args.format,
        loudness=args.loudness,
        sample_rate=args.sample_rate,
        bitrate=args.bitrate,
        title=args.title,
        author=args.author,
        cover=args.cover,
        on_progress=on_progress,
    )
    if result.files is not None:
        # The compliance table is the whole point of the ACX format: every number
        # here was measured on the file that was written, not on the source.
        print(
            f"\nwrote {len(result.files)} files to "
            f"{os.path.dirname(result.path)} ({result.bytes:,} bytes total)\n"
        )
        print(f"  {'file':<44}{'dur':>9}{'LUFS':>9}{'dBTP':>8}{'head':>8}{'tail':>8}  checks")
        for f in result.files:
            m, c = f["measured"], f["checks"]
            flags = " ".join(
                f"{name.replace('_ok', '')}:{'ok' if ok else 'FAIL'}" for name, ok in c.items()
            )
            head = m.get("room_tone_head_dbfs")
            tail = m.get("room_tone_tail_dbfs")
            print(
                f"  {f['file']:<44}{f['duration_ms'] / 1000:8.1f}s"
                f"{m['integrated_lufs']:9.1f}{m['true_peak_dbtp']:8.1f}"
                f"{head if head is None else f'{head:8.1f}'}"
                f"{tail if tail is None else f'{tail:8.1f}'}  {flags}"
            )
        print(
            f"\n  compliance: {result.compliance.upper()}   "
            f"(192 kbps CBR / {result.sample_rate} Hz / mono, "
            f"{ACX_ROOM_TONE_MS} ms room tone head+tail, limiter at "
            f"{ACX_PEAK_CEILING_DBTP - 0.5:.1f} dBTP)"
        )
        return

    print(
        f"wrote {result.path} ({result.bytes:,} bytes, {result.duration_ms / 1000:.1f}s, "
        f"{result.chapters} chapters)"
    )
    if result.measured:
        m = result.measured
        print(
            f"measured loudness: {m['integrated_lufs']:.1f} LUFS / "
            f"{m['true_peak_dbtp']:.1f} dBTP / LRA {m['lra']:.1f}"
        )


if __name__ == "__main__":
    main()
