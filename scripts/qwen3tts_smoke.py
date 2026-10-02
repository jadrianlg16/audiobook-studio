#!/usr/bin/env python
"""Real-GPU smoke for the ``qwen3tts`` engine, driven through the gateway.

Proves the engine end to end and records the numbers in
``engines/qwen3tts/README.md``: load time, VRAM (nvidia-smi, sampled while it
synthesizes), compute vs audio seconds, determinism, what one note does to one
line, and that ``/unload`` gives the VRAM back.

One line, voice ``aiden``, English, taken:

- with no note and with the note ``--note``, on the seed the engine derives from
  voice + text (so the pair differs by the note alone), each **twice** — the
  determinism check hashes the PCM of both;
- again with explicit seeds (``--seeds``, default 1 2 3), because one pair says
  nothing about how much of the difference is the note and how much the seed;
- then one ~25 s paragraph, for speed on a realistic length.

Then ``/unload``, a VRAM read, one reload + the first take again (determinism
across a fresh load), and ``/unload`` again. The engine is left unloaded.

The proxies (duration, RMS, spectral centroid, F0 median/spread, voiced share)
are cheap objective hints, **not** a quality judgment — listen to the WAVs.

    docker compose --profile gpu up -d qwen3tts gateway
    python scripts/qwen3tts_smoke.py --out test-output/qwen3tts

Needs numpy and ``nvidia-smi`` on the host. The gateway URL defaults to
``http://localhost:$GATEWAY_PORT`` (8080 if unset, as in docker-compose.yml).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import threading
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

TEXT = "I heard the footsteps stop right outside my door, and then, very slowly, the handle began to turn."
NOTE = "whispering, trembling with fear"
LONG = (
    "The house had been empty for eleven years when we moved in, and nobody in the village "
    "would tell us why. The estate agent spoke quickly about the roof and the garden and the "
    "view across the valley, and every time my father asked about the previous owners she "
    "found something else to point at. We learned the truth in October, on the first night "
    "the wind came down from the hills and the whole house began, very quietly, to breathe."
)


def vram_used() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True,
    ).stdout
    return int(out.strip().splitlines()[0])


class VramSampler(threading.Thread):
    """Polls nvidia-smi in the background; ``peak`` is the max seen."""

    def __init__(self, every: float = 0.15) -> None:
        super().__init__(daemon=True)
        self.every = every
        self.samples: list[int] = []
        self._halt = threading.Event()  # not `_stop`: Thread uses that name

    def run(self) -> None:
        while not self._halt.is_set():
            self.samples.append(vram_used())
            time.sleep(self.every)

    def stop(self) -> None:
        self._halt.set()
        self.join()


class Gateway:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    def post(self, path: str, body: dict | None = None) -> tuple[bytes, dict[str, str], float]:
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            self.url + path, data=data, method="POST", headers={"Content-Type": "application/json"}
        )
        started = time.perf_counter()
        with urllib.request.urlopen(req, timeout=1800) as resp:
            payload = resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}  # uvicorn lowercases
        return payload, headers, time.perf_counter() - started

    def take(self, text: str, voice: str, note: str | None = None, seed: int | None = None) -> dict:
        body: dict = {"text": text, "voice": voice, "language": "en"}
        if note:
            body["instruction"] = note
        if seed is not None:
            body["seed"] = seed
        wav, headers, wall = self.post("/api/synthesize/qwen3tts", body)
        with wave.open(io.BytesIO(wav)) as w:
            sr = w.getframerate()
            pcm = w.readframes(w.getnframes())
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        return {
            "note": note,
            "seed": seed if seed is not None else "derived",
            "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
            "audio_s": round(len(audio) / sr, 3),
            "gen_s": float(headers.get("x-gen-seconds", 0)),
            "rtf": float(headers.get("x-rtf", 0)),
            "wall_s": round(wall, 2),
            "proxy": proxies(audio, sr),
            "_wav": wav,
        }


def proxies(x: np.ndarray, sr: int) -> dict:
    """Cheap objective hints. NOT a quality judgment."""
    frame, hop = int(0.040 * sr), int(0.010 * sr)
    n = 1 + max(0, (len(x) - frame) // hop)
    frames = np.stack([x[i * hop: i * hop + frame] for i in range(n)])
    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
    active = rms > 0.1 * rms.max()  # speech-ish frames
    spec = np.abs(np.fft.rfft(frames * np.hanning(frame), axis=1))
    freqs = np.fft.rfftfreq(frame, 1 / sr)
    centroid = (spec * freqs).sum(axis=1) / (spec.sum(axis=1) + 1e-12)
    lo, hi = int(sr / 400), int(sr / 70)  # F0 search range 70..400 Hz
    f0 = []
    for f in frames[active]:
        f = f - f.mean()
        ac = np.correlate(f, f, mode="full")[frame - 1:]
        if ac[0] <= 0:
            continue
        seg = ac[lo:hi]
        k = int(np.argmax(seg))
        if seg[k] / ac[0] > 0.5:  # periodic enough to call voiced
            f0.append(sr / (lo + k))
    f0 = np.array(f0)
    semis = 12 * np.log2(f0 / np.median(f0)) if len(f0) else np.zeros(1)
    db = lambda v: round(float(20 * np.log10(v + 1e-12)), 2)  # noqa: E731
    return {
        "duration_s": round(len(x) / sr, 3),
        "rms_dbfs_active": db(np.sqrt(np.mean(frames[active] ** 2))),
        "peak_dbfs": db(np.max(np.abs(x))),
        "spectral_centroid_hz": round(float(centroid[active].mean()), 1),
        "voiced_share": round(len(f0) / max(1, int(active.sum())), 3),
        "f0_median_hz": round(float(np.median(f0)), 1) if len(f0) else None,
        "f0_std_semitones": round(float(semis.std()), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gateway", default=f"http://localhost:{os.environ.get('GATEWAY_PORT', '8080')}")
    ap.add_argument("--out", default="test-output/qwen3tts")
    ap.add_argument("--voice", default="aiden")
    ap.add_argument("--note", default=NOTE)
    ap.add_argument("--seeds", type=int, nargs="*", default=[1, 2, 3])
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    gw = Gateway(args.gateway)

    gw.post("/api/unload/qwen3tts")  # start cold, so the first take times the load
    time.sleep(2)
    report: dict = {"when_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "vram_baseline_mib": vram_used()}
    sampler = VramSampler()
    sampler.start()

    takes = [gw.take(TEXT, args.voice)]
    report["first_take_wall_s_incl_load"] = takes[0]["wall_s"]
    takes.append(gw.take(TEXT, args.voice))
    takes += [gw.take(TEXT, args.voice, args.note), gw.take(TEXT, args.voice, args.note)]
    for seed in args.seeds:
        takes += [gw.take(TEXT, args.voice, seed=seed), gw.take(TEXT, args.voice, args.note, seed=seed)]
    long_take = gw.take(LONG, args.voice)
    sampler.stop()
    report["vram_peak_mib"] = max(sampler.samples)
    report["vram_peak_over_baseline_mib"] = report["vram_peak_mib"] - report["vram_baseline_mib"]
    report["vram_samples"] = len(sampler.samples)

    a1, a2, b1, b2 = takes[:4]
    (out / "a_no_instruction.wav").write_bytes(a1["_wav"])
    (out / "b_whispering_trembling_with_fear.wav").write_bytes(b1["_wav"])
    (out / "long_no_instruction.wav").write_bytes(long_take["_wav"])
    for t in takes[4:]:
        (out / f"{'b' if t['note'] else 'a'}_seed{t['seed']}.wav").write_bytes(t["_wav"])

    gw.post("/api/unload/qwen3tts")
    time.sleep(3)
    report["vram_after_unload_mib"] = vram_used()
    a3 = gw.take(TEXT, args.voice)  # reload, same request
    gw.post("/api/unload/qwen3tts")
    time.sleep(3)
    report["vram_after_final_unload_mib"] = vram_used()
    report["determinism"] = {
        "no_note_twice": a1["pcm_sha256"] == a2["pcm_sha256"],
        "note_twice": b1["pcm_sha256"] == b2["pcm_sha256"],
        "no_note_after_unload_reload": a1["pcm_sha256"] == a3["pcm_sha256"],
        "note_changes_audio": a1["pcm_sha256"] != b1["pcm_sha256"],
    }
    report["takes"] = [
        {k: v for k, v in t.items() if not k.startswith("_")} for t in takes + [long_take, a3]
    ]
    report["takes"][-1]["seed"] = "derived (after reload)"
    (out / "smoke-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps({k: v for k, v in report.items() if k != "takes"}, indent=2))
    print(f"\n{'take':34} {'sha256':12} {'audio':>6} {'gen':>6} {'rtf':>5} | "
          f"{'dur':>5} {'rmsA':>6} {'cent':>6} {'voiced':>6} {'f0med':>6} {'f0sd':>5}")
    for t in report["takes"]:
        p = t["proxy"]
        name = f"{'note' if t['note'] else 'no note'} / seed {t['seed']}"
        if t["audio_s"] > 15:
            name = "paragraph / seed derived"
        print(f"{name:34} {t['pcm_sha256'][:12]} {t['audio_s']:6.2f} {t['gen_s']:6.2f} {t['rtf']:5.2f} | "
              f"{p['duration_s']:5.2f} {p['rms_dbfs_active']:6.1f} {p['spectral_centroid_hz']:6.0f} "
              f"{p['voiced_share']:6.3f} {p['f0_median_hz'] or 0:6.1f} {p['f0_std_semitones']:5.2f}")


if __name__ == "__main__":
    main()
