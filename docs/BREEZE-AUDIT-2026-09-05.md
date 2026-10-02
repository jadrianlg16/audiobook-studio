# Breeze TTS 2 integration audit — 2026-09-05

Reconciles every claim this repo makes about Breeze TTS 2 against the **upstream
source of truth**, which is the BreezeBlue repo cloned into our own built image:

```bash
docker run --rm --entrypoint sh audiobook-studio-breeze:latest -c "cat /opt/breeze-tts/README.md"
```

No network was needed for the primary evidence. `file:line` references below are
paths **inside the image**, at `/opt/breeze-tts/`.

Secondary evidence (labelled where used) came from the Hugging Face model card
`BreezeBlue/Breeze-TTS-2` and BreezeBlue's product page. **Anything sourced only
from the hosted product is marked HOSTED-ONLY and was not allowed to justify a
claim about the open weights.**

The model was never run. No GPU container was started. The checkpoint itself was
not available locally (only the IndexTTS-2 weights were), which bounds what could be
verified — see [Could not verify](#could-not-verify).

## The upstream contract, in one place

`POST /v1/audio/speech` takes exactly six multipart fields and nothing else —
upstream asserts this list in its own test (`tests/test_api.py:32-40`):

| field | type | default | notes |
| --- | --- | --- | --- |
| `text` | form | *required* | `api.py:189` |
| `instruction` | form | `"Speak clearly and naturally."` | `api.py:190` — **always present**, never empty |
| `cfg_scale` | form | `1.0` | `api.py:191`; must be finite and `> 0` (`api.py:210-213`) |
| `ref_audio` | file | none | `api.py:192` |
| `ref_text` | form | `""` | `api.py:193` |
| `seed` | form | `42` | `api.py:194` |

Response: streaming raw mono 24 kHz s16le PCM, no WAV header, with
`X-Sample-Rate` and `X-Sample-Format: s16le` headers (`api.py:281-287`).

## Reconciliation table

| # | Claim we made | Upstream evidence | Verdict | Change made |
| --- | --- | --- | --- | --- |
| 1 | Inline events are written `(sigh)`, parenthesised | `README.md` Highlights: "use parentheses in English … `(laugh)`, `(cough)`, `(clears throat)`, and `(sigh)`" | **confirmed** | none |
| 2 | Events are language-specific (EN parens / ZH brackets) — *we never said this* | same line: "use square brackets in Chinese, such as `[笑]`, `[咳嗽]`, `[清嗓子]`, and `[叹气]`" | **wrong by omission** | Stated in `engines/breeze/README.md`; `director.md`'s "never square brackets" rule now scoped to English so it does not misfire on a Chinese book |
| 3 | The engine understands 28 events — `(chuckle)`, `(whisper)`, `(pause)`, `(scoff)`, `(sneeze)`… | **No event vocabulary exists anywhere in the open-weight release.** `grep -rn "sigh\|laugh\|cough\|clears throat" /opt/breeze-tts --include=*.py --include=*.md --include=*.json` returns hits **only** in `README.md`. No tokenizer special tokens, no vocabulary file, no lookup table. Events are ordinary text tokens; only the four README examples are attested | **wrong** | `_DEFAULT_AUDIO_TAGS` pruned 28 → the 4 attested tags. `BREEZE_AUDIO_TAGS` override kept |
| 4 | The extra 24 tags come from "BreezeBlue's hosted audio-tag docs" | The product page shows `[gasps]` and `[pants]` — **square brackets, English, plural spellings**, i.e. neither our syntax nor our spellings. HOSTED-ONLY | **wrong** | Removed the claim; README now says the hosted product uses a *different* tag syntax and is not evidence for the open weights |
| 5 | Instruction field is named `instruction` | `api.py:190`; `templates.py:49` | **confirmed** | none |
| 6 | Instruction and reference audio can be combined (voice direction) | `api.py:227-231` selects template `ref_edit_tata`; `templates.py:83-92` puts `<ins_bos>{instruction}<ins_eos>` **after** the reference pair | **confirmed** | none |
| 7 | Sending no instruction gives you a "pure clone" | Server-side `instruction` **defaults to a non-empty string** (`api.py:190`) and `prepare_inputs` **rejects a falsy instruction** (`templates.py:117`, `273-281`). With a reference the server *always* uses `ref_edit_tata`. The pure-clone template `ref_clone_tata` (`templates.py:74`) is **unreachable over HTTP** — it is only ever the CFG negative branch (`templates.py:95-96`) | **wrong** | Mode table in `engines/breeze/README.md` and `tts.py` corrected: omitting the instruction yields direction-with-a-neutral-note, not a distinct clone path |
| 8 | Instructions are capped at 400 chars because "long instructions measurably dilute adherence" | **No length limit of any kind upstream.** The only real ceiling is `MAX_SEQ_LEN = 2048` tokens for the whole prompt (`api.py:38`, `infer.py:26`) — instruction + text + reference audio tokens share it | **unsupported** (the cap is this project's policy; nothing upstream backs the stated reason) | Cap kept at 400 (it is a sane budget against the 2048 shared ceiling) but re-labelled in `director.py` as our own policy, with the real upstream constraint named |
| 9 | `ref_text` is required whenever `ref_audio` is sent | `api.py:216-221`: 400 when `has_reference != bool(ref_text)` | **confirmed** | none |
| 10 | *(not claimed)* the rule is symmetric — `ref_text` without `ref_audio` is also a 400 | same line, `!=` in both directions | **wrong by omission — real bug** | `tts.py` now drops a stray `ref_text` when there is no clip, instead of shipping a request upstream rejects |
| 11 | `emotion` 0..1 → `cfg_scale` 1..8, "0.5 lands on their recommended value" | Recommended value is `4` (`README.md` Voice Design / Voice Direction). Our own mapping gives **4.5**, not 4 | **wrong** (the arithmetic claim) | Comment in `tts.py` and the test's comment corrected to say 4.5 is *near* 4, not equal to it. Mapping itself left alone — it is a deliberate bridge |
| 12 | `cfg_scale` clamped to 1..10 | Upstream requires only finite and `> 0` (`api.py:210-213`); the fast-path warmup profile additionally refuses `< 1.0` (`warmup_profile.py:150`) | **confirmed** (our floor of 1.0 is the safer of the two) | Rationale documented |
| 13 | A non-standard cfg like 4.5 is risky on the fast path (implied by "recommended 4") | `cudagraph/backbone_graph.py:32`: "guidance_scale is runtime-mutable without recapture". Graphs bucket by CFG *shape* (`no_cfg` / `single_cfg`), not value (`fast_streaming.py:91-102`) | **confirmed safe** | Noted in README so nobody "fixes" 4.5 back to 4 |
| 14 | `speed` is ignored because Breeze has no speed parameter | The six-field signature is exhaustive and upstream asserts it (`tests/test_api.py:32-40`). No pace/rate/speed field exists | **confirmed** | none |
| 15 | Output is 24 kHz mono s16le | `README.md` Streaming API; `api.py:281-287` | **confirmed** | none, but see #16 |
| 16 | Our `SR = 24_000` is a constant | Server reports it dynamically from `model.config.codec_config.sampling_rate` (`fast_streaming.py:208-209`) and returns it in `X-Sample-Rate`, which we ignore | **confirmed for this checkpoint**, fragile in principle | Documented as a known assumption; not changed (changing it needs a live server to test) |
| 17 | Languages: English + Chinese only | `README.md` Highlights: "Generates natural English and Chinese speech with a single model" | **confirmed** | none |
| 18 | Weights are research/non-commercial **including self-hosted outputs**; code is Apache-2.0 | `README.md` License and Responsible Use, verbatim on both points | **confirmed** | none |
| 19 | VRAM ~7.7 GiB eager / ~14.4 GiB `--fast-all`, 12 GB minimum | `README.md` Requirements | **confirmed** | none |
| 20 | *(not claimed)* the server is single-concurrency | `api.py:38, 197-201`: a module-level lock, **HTTP 409** on a second in-flight request | **wrong by omission** | Documented in `engines/breeze/README.md`; matters because a bake-off fans out |
| 21 | *(not claimed)* every text is prefixed with a speaker tag | `api.py:226` hardcodes `"speaker": "S0"`; `templates.py:31-37` emits `[S0]` before ref text, instruction block, and text | **confirmed, previously undocumented** | Documented — it is why a Chinese `[…]` event is unambiguous and why we must not send our own `[S0]` |

## Fixes applied

- **`engines/breeze/tts.py`** — `_DEFAULT_AUDIO_TAGS` cut from 28 speculative
  entries to the 4 attested ones; stray `ref_text` without a clip is dropped
  rather than sent (upstream 400); cfg comment corrected (4.5, not 4); module
  docstring's three-mode table corrected for #7.
- **`engines/breeze/README.md`** — tag section rewritten around "four attested,
  everything else is yours to prove"; hosted-vs-open-weights syntax difference
  called out; clone/direction nuance, 409 single-concurrency, `[S0]` prefix and
  the 2048-token ceiling added.
- **`gateway/app/prompts/director.md`** — instruction guidance rewritten to the
  shape upstream actually demonstrates (see below); bracket rule scoped to
  English. **The anti-paraphrase rule is untouched.**
- **`gateway/app/services/director.py`** — `_MAX_INSTRUCTION_CHARS` rationale
  corrected to name our policy and the real 2048-token ceiling.
- **`pipeline/direct.py`, `scripts/try_director.py`** — hardcoded fallback tag
  lists (`(gasp)`, `(whisper)`, `(pause)`) reduced to the attested four, so the
  offline fake director cannot teach us to expect tags the model never saw.
- **`.env.example`** — repeated tag claims corrected.
- **`engines/breeze/tests/`** — tests pinning the pruned list, the symmetric
  `ref_text` guard, and the corrected cfg arithmetic.

### Why `director.md`'s instruction section changed

Upstream demonstrates two distinct instruction shapes, and we were blurring them:

- **Voice design** (no reference) — *who is speaking*: "A warm, thoughtful young
  woman with a clear voice and a calm, reflective delivery." (`README.md`, Voice
  Design).
- **Voice direction** (with reference) — *how to deliver this line*: "Speak
  slowly with a restrained, serious tone." (`README.md`, Voice Direction).

`pipeline/render.py:156-161` composes them as `"{voice_design}. {instruction}"` —
identity first, delivery second. That ordering matches upstream's own design
example, which puts identity before delivery inside one sentence. It is
**consistent with** upstream, not **specified by** it (upstream never composes
the two itself), so it is left as-is and labelled.

The concrete prompt change: the director must not put speaker identity into
`instruction`, because in direction mode identity comes from the reference clip
and from `voice_design` — that rule already existed and is now grounded in
upstream's split rather than asserted. Added: pace belongs in the instruction
(upstream's own example directs pace in prose, and there is no speed parameter —
#14), and events are placed in `text`, never described in `instruction`.

## Fixed separately, after the audit

The audit was scoped to the Breeze adapter, its docs and the director. Two copies
of the old 28-tag list lived outside that scope and were corrected separately to
the same four tags:

- `pipeline/studio/engines.py` (`_BREEZE_TAGS`) — the studio's static fallback,
  used whenever the gateway is not answering. While it was stale, that path still
  offered the director `(whisper)`, `(pause)`, `(gasp)` and the rest. The gateway's
  live list still wins when it answers.
- `frontend/src/studio/api/mock.ts` (`BREEZE_TAGS`) — UI mock data. Lower stakes
  (it never reaches an engine), but it would have misled whoever read it next.

`gateway/tests/test_director.py` uses `(pause)` as a synthetic tag when testing
the canonicaliser. That is **correct and should stay** — those tests exercise
bracket-repair logic generically and make no claim about Breeze's vocabulary.

## Could not verify

- **HYPOTHESIS — that the four attested tags are the *complete* set the
  checkpoint recognises.** The four are attested as *examples* ("such as"), not
  as a closed vocabulary. There is no vocabulary file to enumerate. A wider set
  may work; our list is now "what upstream shows", which is the defensible floor,
  not a proven ceiling. `BREEZE_AUDIO_TAGS` exists exactly for this.
- **HYPOTHESIS — that an unrecognised tag is read aloud as literal words.** Our
  README asserts this. It is plausible (events are ordinary text tokens, so an
  unknown one is just text) but nothing upstream states it and we did not run the
  model. Now labelled in the README rather than asserted.
- **The tokenizer's added/special tokens could not be inspected.** The checkpoint
  is bind-mounted, not baked into the image, and was not available locally. `<ins_bos>` / `<ins_eos>` /
  `<|AUDIO|>` are named in `templates.py:12-15`, but whether the tokenizer also
  carries event tokens is unresolved. **Fetching the checkpoint would settle
  claim #3 definitively.**
- **No behavioural claim about audio quality, adherence, or the emotion→cfg
  bridge was tested.** The model was not run for this audit.
- **HOSTED-ONLY material is not evidence about the open weights** and was used
  only to *retract* claim #4, never to support anything.
