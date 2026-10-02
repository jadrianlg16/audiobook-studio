# Direction A/B — does the director's note make a passage sound better?

The experiment that decides whether the director is worth more work. It targets Qwen3-TTS
because that is the directable engine that fits an 8 GB card and whose weights are
Apache-2.0 (`engines/qwen3tts/README.md`).

**Status: not scored.** The takes have been rendered, but no listener has done the
blind test yet, so there is no result. Nothing in this repository claims that the
director improves narration.

## Method

- **Passages:** eight, in `passages.json` — six public-domain cuts from Project
  Gutenberg (Poe, Dickens ×2, Brontë, Stoker, Austen) and two repo samples, chosen
  for different registers (dread, joy, grief, defiance, unease, irony, resolve,
  weariness). The register label is for analysis only.
- **Notes:** the product path — `pipeline.direct` through the gateway, director
  `gemma3:12b` (Ollama), target engine `qwen3tts`, voice `aiden`, every passage
  built as its own single-voice book. The scene note is stamped on every line.
- **Conditions** (only `render.instruction` differs; speed pinned to 1.0):
  **A** no note, **B** the director's note, **C** the director's one-word emotion.
- **Control of chance:** the engine seeds each line from `sha256(voice + text)`, so
  A, B and C of a line start from the same random state. Takes are RMS-matched to
  −20 dBFS so loudness is not a clue. File names and order are random; the key is
  outside the served folder.
- **Judging:** one listener, blind. Part 1 = A vs B per passage: which take to keep,
  a 1–5 rating per take, then (after revealing the note) which take follows it.
  Part 2 (optional) = B vs C.
- **Threshold** (set before any listening): B kept on at least 6 of 8 passages (75 %) →
  direction is worth the investment on the licence-clean path. Near a coin flip →
  the director screens are lab tooling until better hardware or a better engine.

Run it (needs the GPU profile, `qwen3tts` and an Ollama model):

```bash
python scripts/direction_ab.py prepare --engine qwen3tts --voice aiden --model gemma3:12b
python scripts/direction_ab.py render
python scripts/direction_ab.py serve      # open the printed URL and listen
python scripts/direction_ab.py score
```

Everything is written under `output/direction-ab/`, which is git-ignored.
`pipeline/tests/test_direction_ab.py` covers the harness offline: the three takes differ
only in the note, loudness matching without clipping, and scoring that unblinds each
answer to its condition.

## Observations from the render step (2026-09-27, before any listening)

These are facts about the pipeline, not about audio quality.

- **The director often gives no note.** On the first try, 2 of 8 passages got a note
  for qwen3tts; the rest came back `instruction: null` (4, the prompt allows it) or a
  transient gateway 400 (2). Within three tries all 8 got one, which is why
  `prepare` retries a passage the director left without a note. A real book pass on
  this model would leave most scenes undirected unless it retries.
- **Some notes miss the passage.** The Dracula note ("theatrical lilt and
  exaggerated politeness") describes the Count while every line is the narrator's;
  a scene-level note stamped on narration can direct the wrong person.
- **Renders are sane by duration:** 64 lines × 3 conditions, median 3.0 words/s,
  no line outside 45–190 % of that rate. This does not rule out mispronunciations.

## Results

None yet. The blind listening test has not been run.
