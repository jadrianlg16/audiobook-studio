# Breeze TTS 2 engine

BreezeBlue's Breeze TTS 2, self-hosted. Announced 7 Aug 2026; open weights and
PyTorch inference published 25 Aug 2026.

It is the only engine in this repo that takes a **written performance note**
alongside the text, which is why it exists here: the AI director can stop picking
labels from a dropdown and start writing actual direction.

## Read this before you use the output

The **code** in this directory is ours. The **weights** are not, and their licence
covers what they produce:

> Model weights, checkpoints, adapters, derivative models, and **self-hosted
> outputs** are governed by the BreezeBlue Research and Non-Commercial License.

So: fine for evaluating, comparing, and tuning the director. **Not** licensed for
an audiobook you sell or publish commercially. BreezeBlue's hosted API is the
commercial path (their pricing page lists commercial use on every tier, free
included) — and that would be a different adapter, since it speaks a different
protocol. This one drives the local server.

Upstream's own source code is Apache-2.0; that is not what restricts you.

## What it can do

| Mode                | Send                                  | Get |
| ------------------- | ------------------------------------- | --- |
| **Voice design**    | `instruction` only                    | A voice invented to match the description |
| **Voice clone**     | `reference_audio_b64` + `reference_text` | That speaker |
| **Voice direction** | both                                  | That speaker, performed as directed |

Plus **inline vocal events** — `(sigh)`, `(laugh)`, `(clears throat)`, `(cough)`
— written directly into the text.

"Clone" is our framing, not a separate upstream path. Upstream's `instruction`
field defaults to `"Speak clearly and naturally."` and its template layer refuses
an empty one, so **whenever a reference clip is present the server builds the
direction prompt** — sending no instruction gets you direction with a neutral
note. The table is still the right way to think about it; it just isn't three
code paths upstream. (Audit 2026-09-05.)

Cloning **requires the reference clip's exact transcript**. The rule is upstream's
and it is **symmetric** — the server 400s on a clip without a transcript *and* on
a transcript without a clip. The adapter returns a 400 for the first (a clone
without a transcript is noticeably worse, so failing loudly beats sounding bad)
and silently drops the second, since a transcript with no audio is just a voice
design request with a stray field. That is why `SynthRequest` grew a
`reference_text` field.

### The reference clip is not the only thing in the prompt

Upstream prefixes every text segment with a speaker tag — it hardcodes `S0`, so
your text is rendered as `[S0]…`. Two consequences: don't send your own `[S0]`,
and the whole prompt (instruction + text + the reference clip's audio tokens)
shares one **2048-token** ceiling, with 1500 tokens the cap on generated audio.
Long instructions eat the same budget as long text.

### One request at a time

The upstream server takes a process-wide lock and answers **HTTP 409** to a second
in-flight request rather than queueing it. The adapter turns that into a named
error instead of a bare status code. It matters here because a bake-off fans out
across engines — Breeze is the one that cannot take it. One server per concurrent
request, or serialize.

Bilingual **English + Chinese only** in the open weights. (BreezeBlue's hosted
service covers more languages; the checkpoint you run here does not.)

## Hardware

| | |
| --- | --- |
| Parameters | 3B |
| Output | 24 kHz mono |
| VRAM, eager | ~7.7 GiB |
| VRAM, `BREEZE_FAST=1` | ~14.4 GiB |
| Upstream minimum | 12 GB recommended |

**An 8 GB card is below upstream's recommended minimum.** ~7.7 GiB on an 8192 MiB
card leaves under 400 MiB for everything else, so expect CUDA OOM — especially
with another engine resident. Two ways out:

1. Run it on a bigger machine and set `BREEZE_SERVER_URL` (below).
2. Try a community INT8 build. Unverified here; treat as an experiment.

## Setup

Download the checkpoint once on the host — it is bind-mounted, never baked into
the image:

```bash
huggingface-cli download BreezeBlue/Breeze-TTS-2 --local-dir ./models/breeze-tts-2
```

Then bring it up with the GPU profile:

```bash
docker compose --profile gpu up -d breeze
```

## Two modes

The adapter drives BreezeBlue's **documented HTTP contract**
(`POST /v1/audio/speech`, multipart in, raw 24 kHz s16le PCM out) rather than
importing `breeze_infer` internals, because upstream documents a CLI and a server
but no stable Python API.

**Managed** (default, `BREEZE_SERVER_URL` blank) — the container spawns
`python -m breeze_infer.api` itself and owns it. `/unload` kills the process,
which frees VRAM more completely than dropping torch references does.

**Remote** (`BREEZE_SERVER_URL=http://big-box:7860`) — the adapter only proxies.
This is how you keep the stack on one machine while the model runs on another,
and it is also how the test suite runs the whole adapter with no GPU at all.

## Environment

| Variable | Default | Why |
| --- | --- | --- |
| `BREEZE_SERVER_URL` | *(blank)* | Blank = spawn locally. Set = proxy to that server. |
| `BREEZE_MODEL_DIR` | `/models/breeze-tts-2` | Checkpoint location. |
| `BREEZE_REPO_DIR` | `/opt/breeze-tts` | Where the image cloned upstream. |
| `BREEZE_FAST` | `0` | `1` adds `--fast-all`: ~2x faster, ~2x VRAM. |
| `BREEZE_HOST` / `BREEZE_PORT` | `127.0.0.1` / `7860` | Managed-mode bind. |
| `BREEZE_WARMUP` | `1` | Run one real inference at load. See below. |
| `BREEZE_STARTUP_TIMEOUT` | `600` | Seconds to wait for the server. |
| `BREEZE_REQUEST_TIMEOUT` | `900` | Per-synthesis timeout. |
| `BREEZE_AUDIO_TAGS` | *(built-in list)* | Pin the tags that actually work. |

### Why warm-up does a real inference

A port that accepts connections proves nothing — a server with missing weights or
a mismatched CUDA build will happily listen and then fail on the first real
request. `BREEZE_WARMUP=1` synthesizes one short line at load time so that class
of failure surfaces immediately, not four chapters into a book.

### About the tag list

The built-in list is exactly four: `(laugh)`, `(cough)`, `(clears throat)`,
`(sigh)`. Those are the only inline events the open-weight release attests, in its
README's Vocal Events highlight and its own CLI examples.

**There is no event vocabulary in the open weights to check against.** Audited
2026-09-05: no tokenizer special tokens, no lookup table, no config list — a grep
for those words across the whole upstream repo hits the README and nothing else.
Vocal events are ordinary text tokens. So the four are what upstream *shows*, not
a set anything enumerates.

This list previously carried 28 entries — `(whisper)`, `(pause)`, `(scoff)` and
friends — sourced from BreezeBlue's **hosted** product docs. That was wrong twice:
the hosted service is a different model, and it writes English events in *square
brackets* with different spellings (`[gasps]`, `[pants]`), so it wasn't even
evidence for the syntax. Don't reintroduce them from that source.

Upstream says "such as", not "one of", so treat four as a defensible floor rather
than a proven ceiling. A/B on your own checkpoint and pin what survives:

```bash
BREEZE_AUDIO_TAGS="(sigh),(laugh),(clears throat),(cough),(gasp)"
```

**Chinese uses square brackets** — `[笑]`, `[咳嗽]`, `[清嗓子]`, `[叹气]`. The
built-in list is English; a Chinese book needs `BREEZE_AUDIO_TAGS` set to those.

HYPOTHESIS: a tag the model doesn't know is read aloud as literal words. That
follows from events being ordinary text tokens, but nothing upstream says so and
we have not run it. Assume it, don't rely on it.

The director is only ever offered the tags this list advertises.

## Parameter mapping

| Shared contract | Breeze | Note |
| --- | --- | --- |
| `instruction` | `instruction` | The real feature. |
| `emotion_label` | `instruction` | Fallback, so the old preset UI still does something. |
| `emotion` (0..1) | `cfg_scale` (1..8) | 0.5 → **4.5**, near upstream's recommended 4 — near, not equal. |
| `extra.cfg_scale` | `cfg_scale` | Explicit override. |
| `reference_audio_b64` | `ref_audio` | |
| `reference_text` | `ref_text` | Required whenever `ref_audio` is sent; dropped when it isn't. |
| `seed` | `seed` | Omitted when unset; upstream then uses 42. |
| `speed` | — | **Ignored.** Breeze has no speed parameter — ask for pace in the instruction ("speak slowly"), which is what the model is built for. |

`emotion` maps to `cfg_scale`, which is instruction *adherence*, not
expressiveness. They are related but not the same thing; it is a bridge so the
existing slider does something sensible, not a native mapping.

Upstream defaults `cfg_scale` to **1.0** and validates only that it is finite and
`> 0` — there is no documented upper bound. Our floor of 1.0 is deliberate: the
fast path's warmup profile refuses values below 1.0, so 1.0 satisfies both
constraints. Don't "round" 4.5 to 4 for the fast path either — CUDA graphs bucket
by CFG *shape* (`no_cfg` / `single_cfg`), and upstream documents `guidance_scale`
as runtime-mutable without recapture, so any value in range is safe.

The output sample rate is reported by the server (`X-Sample-Rate`, and a
`codec_config.sampling_rate` on the model) rather than fixed in its code. Our
`SR = 24_000` matches what upstream documents and what this checkpoint produces,
but it is a constant on our side and we ignore the header. Fine today; worth
knowing if BreezeBlue ever ships a checkpoint at another rate.

## Note on `BAKEOFF_UNLOAD`

With `BAKEOFF_UNLOAD=1` the gateway asks each engine to free its model after every
bake-off synthesis. For Breeze in managed mode that kills and respawns the server,
which means reloading a 3B checkpoint on the next run. Correct, but slow — if you
are iterating on Breeze specifically and have the VRAM, turn it off.

## Tests

```bash
pytest engines/breeze/tests
```

No GPU, no weights, no network. A stub speaks the same documented contract, so
these cover the things that actually break — multipart encoding, PCM framing,
the missing-transcript guard — on any machine.
