# Qwen3-TTS engine

Alibaba Qwen's **Qwen3-TTS**, self-hosted, run in-process through the official
`qwen-tts==0.1.1` package. The default checkpoint is
[`Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice):
nine named voices, each of which takes an optional **written performance note**.

It is the second engine here that takes prose direction (after Breeze) and the
first whose weights you may use for a book you publish.

## Licence — weights, code and output

| | Licence | Source (checked 2026-09-26) |
| --- | --- | --- |
| Weights | **Apache-2.0** | Model card metadata `license: apache-2.0` ([card](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice), [API](https://huggingface.co/api/models/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice)); the same tag on the 0.6B-CustomVoice, 1.7B-VoiceDesign, 1.7B-Base and 0.6B-Base repos. None of them is gated. |
| Code | Apache-2.0 | `qwen-tts` package metadata; [QwenLM/Qwen3-TTS `LICENSE`](https://github.com/QwenLM/Qwen3-TTS/blob/main/LICENSE) |
| Output | **no terms** | Neither the model card nor the GitHub README says anything about generated audio, commercial use or prohibited uses (grep for `licen`, `commercial`, `output`, `misuse`, `prohibit`, 2026-09-26). Apache-2.0 licenses the weights and puts no condition on what they produce. |

So unlike Breeze, a book routed through `qwen3tts` carries **no** licence note
(`pipeline/cast.py::non_commercial_engines()` leaves it out).

Two things the sources do *not* say, so don't assume them: the HF repo has no
separate `LICENSE` file (the licence is the metadata tag), and nothing says who
the nine speakers are or whether their likeness carries any terms. If you sell a
book voiced by one of them, that is the one open question. This is a summary of
what the pages say, not legal advice.

## What it can do

| Shared contract | Qwen3-TTS | Note |
| --- | --- | --- |
| `text` | `text` | Verbatim. |
| `voice` | `speaker` | Case-insensitive. Unknown → **400** naming the real ones. Empty → `QWEN_TTS_DEFAULT_VOICE` (`aiden`). |
| `instruction` | `instruct` | Stripped. Absent / blank → `None` (no note). |
| `language` | `language` | ISO code (`en`), the model's own name (`English`) or `auto`. Unsupported → **400**. |
| `seed` | RNG seed | Optional — see [Seeding](#seeding). |
| `extra.temperature`, `top_k`, `top_p`, `do_sample`, `repetition_penalty`, `max_new_tokens`, `subtalker_*` | generation kwargs | Defaults come from the checkpoint's `generation_config.json`. Other `extra` keys are ignored. |
| `speed` | — | **Ignored.** The model has no rate control. Put pace in the note ("speak slowly"): the model card lists speaking rate among what `instruct` controls. Same precedent as Breeze. |
| `emotion`, `emotion_label`, reference clips | — | Ignored, and not declared in `Capabilities`. |

What `/info` declares, and why:

- `instruction: true` — the note is sent as `instruct`. Reported **false** on the
  0.6B CustomVoice checkpoint, because upstream silently drops `instruct` there.
- `multi_speaker: true`, `voices` = the checkpoint's own speaker list (read from
  its `config.json` in the HF cache, or from the loaded model), default first.
- `languages` = the checkpoint's own list as ISO codes: en, zh, ja, ko, de, fr, ru,
  pt, es, it. (Beijing and Sichuan dialects are reached through the `dylan` and
  `eric` speakers, not requested as languages — upstream does the same.)
- `audio_tags: []` — nothing in the weights, the model card or the package
  attests an inline event vocabulary like `(sigh)`. The Breeze lesson in
  [`docs/BREEZE-AUDIT-2026-09-05.md`](../../docs/BREEZE-AUDIT-2026-09-05.md): advertise only what the weights attest. An empty list means the
  director never inserts one.
- `cloning: false` — see [Not wired](#not-wired).

Before the weights are downloaded, `/info` reports the default checkpoint's
published facts (`engines/qwen3tts/tests` pins them against its real config).

### Voices

Upstream: *"We recommend using each speaker's native language for the best
quality. Of course, each speaker can speak any language supported by the model."*

| id | Card description | Native language |
| --- | --- | --- |
| `aiden` (default) | Sunny American male voice with a clear midrange | English |
| `ryan` | Dynamic male voice with strong rhythmic drive | English |
| `vivian` | Bright, slightly edgy young female voice | Chinese |
| `serena` | Warm, gentle young female voice | Chinese |
| `uncle_fu` | Seasoned male voice with a low, mellow timbre | Chinese |
| `dylan` | Youthful Beijing male voice with a clear, natural timbre | Chinese (Beijing dialect) |
| `eric` | Lively Chengdu male voice with a slightly husky brightness | Chinese (Sichuan dialect) |
| `ono_anna` | Playful Japanese female voice with a light, nimble timbre | Japanese |
| `sohee` | Warm Korean female voice with rich emotion | Korean |

Only two voices are English natives, and both are male. An English book with
female characters has to use a non-native speaker — worth an A/B before casting.

### What `instruction` does

Upstream wraps the note as a user turn (`<|im_start|>user\n{instruct}<|im_end|>`)
placed before the text, and the model card says it controls *"timbre, emotion,
and prosody"*. The speaker identity comes from the named voice; the note steers
how that voice performs the line. See [Measured](#measured-on-the-rtx-3060) for
what one note did to one line.

## Seeding

Sampling is on by default (`do_sample=true`, temperature 0.9 for both the talker
and the sub-talker), so two takes of the same line differ. That would swamp an
A/B of direction, so every request is seeded:

- `req.seed` if the caller sets one;
- otherwise `sha256(f"{voice}\0{text}")`, first 4 bytes, masked to 31 bits — on
  the **resolved** voice, so an empty `voice` and the default's name are the same take.

The instruction is deliberately **not** in the hash: the same line with and
without a note starts from the same random state, so the note is the only
variable. The seed is logged per request (`docker compose logs qwen3tts`).
Changing the formula changes every take of every book — the tests pin it.

## Measured on the RTX 3060

2026-09-27 (UTC), RTX 3060 8 GB with the Windows desktop holding ~1.9 GB, image
`audiobook-studio-qwen3tts` (torch 2.9.1+cu128, bf16, SDPA), checkpoint revision
`0c0e305`. Every request went through the gateway (`POST /api/synthesize/qwen3tts`),
voice `aiden`, language `en`. Reproduce with:

```bash
docker compose --profile gpu up -d qwen3tts gateway
python scripts/qwen3tts_smoke.py --gateway http://localhost:8080 --out test-output/qwen3tts
```

| | |
| --- | --- |
| Download | 4.3 GB on disk, 426 s on this connection |
| Load (`from_pretrained` from the HF cache) | **17.4–18.8 s** (n = 7 loads) |
| VRAM while loaded | **+4,860 MiB** over the desktop's 1,894 MiB (peak 6,754 MiB; `nvidia-smi` every ~0.15 s, 910 samples) — weights, CUDA context and PyTorch's cache together. A 24.6 s paragraph did not raise it. |
| VRAM after `/unload` | back to **1,894 MiB** (baseline) |
| Compute ÷ audio (engine `X-RTF`) | **1.50–1.83** on 6.5–7.6 s lines (n = 11), **1.66** on a 431-character paragraph (24.6 s of audio in 40.7 s) — slower than real time |
| GPU utilisation mid-synthesis | 36 % in one `nvidia-smi` read — the per-frame decode loop, not the GPU, looks like the bottleneck |

HYPOTHESIS (extrapolated from these lines, not measured on a book): a 10-hour
audiobook is ~15–18 hours of rendering on this card.

**Determinism.** Identical requests produce **bit-identical PCM** (SHA-256 of the
samples): twice within a load, after `/unload` + reload, and across six separate
model loads over four script runs — e.g. `c334b25d…` (no note), `43e63cf7…` (note),
`811d2587…` (paragraph), and the explicit-seed takes too. That is on this GPU,
driver and image; another card may pick different kernels and differ.

**What the note did** (`"whispering, trembling with fear"`, same line, same
voice; proxies from `scripts/qwen3tts_smoke.py` — hints, **not** a quality
judgment; listen to the WAVs):

| seed | take | duration s | active RMS dBFS | centroid Hz | voiced share | F0 median Hz | F0 spread (semitones) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| derived | no note | 7.60 | −23.0 | 1706 | 0.775 | 101.7 | 1.53 |
| derived | note | 6.56 | −21.1 | 1501 | 0.811 | 107.6 | 2.23 |
| 1 | no note | 6.48 | −19.9 | 1344 | 0.865 | 130.4 | 3.69 |
| 1 | note | 7.52 | −20.1 | 1892 | 0.823 | 151.4 | 1.95 |
| 2 | no note | 7.36 | −21.2 | 1238 | 0.889 | 113.7 | 2.80 |
| 2 | note | 7.12 | −17.9 | 1762 | 0.849 | 162.2 | 1.69 |
| 3 | no note | 7.20 | −22.1 | 1598 | 0.837 | 123.7 | 3.50 |
| 3 | note | 7.04 | −17.0 | 1719 | 0.863 | 190.5 | 2.07 |

Read it as:

- The note **changes the take** every time (different audio, same seed).
- The one direction all four pairs share: **median F0 goes up** with the note
  (+6 to +67 Hz). Loudness goes up in three of four.
- Nothing here looks like a **whisper** — a whisper would be quieter and *less*
  voiced, and these takes are neither. Whether "trembling with fear" is audible
  is a listening question.
- The **seed alone** moves Aiden's median F0 from 102 to 130 Hz with no note at
  all, and flips the direction of the centroid and F0-spread "effects". One
  pair proves nothing: compare notes over several seeds (or many lines).

That last point matters beyond A/B tests. Each line is seeded from its own
text, so HYPOTHESIS: a character's pitch wanders from line to line within a
book, even on one named voice. Listen to a scene before committing a cast.

## Not wired

- **Cloning** needs the separate *Base* checkpoint (another ~4.5 GB) plus the
  reference transcript — a second model or a model swap on an 8 GB card, never
  run here. `cloning` is false. A Base or VoiceDesign checkpoint in
  `QWEN_TTS_MODEL` is refused at load with a message naming the problem.
- **Voice design** (the *VoiceDesign* checkpoint) would fit the repo's
  `voice_design` casting, but it builds the voice from the description on every
  call. HYPOTHESIS (never run): seeded per line, each line of one character would
  come out as a slightly different voice. Upstream's own advice for a reusable
  designed voice is to design one clip and then clone it with the Base model.
  Not wired.
- **Streaming**: off. `non_streaming_mode=True` always.

## Limits

- **Per-call length:** the checkpoint caps generation at `max_new_tokens` = 8192
  codec frames; the 12 Hz tokenizer runs at 12.5 frames/s (24 kHz / 1920), so
  about **655 s of audio per call**. The contract allows 20,000 characters of text.
  The pipeline already chunks lines, which is the right shape for this model.
- **One request at a time** per container (the engine app's `op_lock`), like
  every engine here.
- `flash-attn` is not built into the image, so `qwen-tts` prints a warning at
  import. It comes from the 25 Hz tokenizer's Whisper encoder, which the 12 Hz
  checkpoints never use; the main model runs on PyTorch SDPA (`QWEN_TTS_ATTN`).
  The `sox` warning is gone because the image installs the `sox` binary.
- **Hybrid casting** (`pipeline/cast.py`) only knows Kokoro's named voices, so a
  character it moves onto `qwen3tts` gets a voice *description* prefixed to its
  note and the **default voice** (`aiden`). Pick the voice in the cast instead
  (the studio's catalog lists all nine, so `PATCH` accepts them).

## Setup

Weights land in the shared HF cache (`HF_CACHE_DIR`, mounted at `/cache`) on the
first synthesis. To download once, ahead of time (~4.5 GB):

```bash
docker compose --profile gpu run --rm --no-deps --entrypoint hf qwen3tts \
  download Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice
docker compose --profile gpu up -d qwen3tts gateway
```

Nothing is baked into the image. The Dockerfile starts from
`pytorch/pytorch:2.9.1-cuda12.8-cudnn9-runtime` — the torch the Breeze image runs
`qwen-tts` on — and pins torch/torchvision/torchaudio to what the base shipped
with a pip constraint, so no dependency can upgrade torch and strand torchvision
(the trap `engines/breeze/Dockerfile` describes).

## Environment

| Variable | Default | Why |
| --- | --- | --- |
| `QWEN_TTS_MODEL` | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` | Any CustomVoice checkpoint: HF repo id, or a directory the container can see. |
| `QWEN_TTS_DEFAULT_VOICE` | `aiden` | Used when a request names no voice. Falls back to `aiden` if unknown. |
| `QWEN_TTS_DEVICE` | `cuda:0` if available, else `cpu` | `device_map` for `from_pretrained`. |
| `QWEN_TTS_DTYPE` | `bfloat16` | Any `torch` dtype name. |
| `QWEN_TTS_ATTN` | `sdpa` | `attn_implementation`. `flash_attention_2` needs flash-attn in the image. |
| `LAZY_LOAD` | `1` (compose) | 0 VRAM until the first synthesis. `/unload` frees it again. |

## Tests

```bash
pytest engines/qwen3tts/tests
```

No GPU, no weights, no network, no real `torch`: fake `torch` and `qwen_tts`
modules are injected, so the adapter's real `_load` (checkpoint, bf16, SDPA,
type guard) runs through the real engine app (lazy load, `/unload`, reload).
They pin what the model would be *asked*: speaker, language, note, seed. Whether
a note audibly changes the delivery is only answered on a GPU — above.
