# Audiobook pipeline

Turn a book file into a **playable audiobook** — narrated by Kokoro, with a
read-along web player, chapters, and bookmarks.

This package is the orchestration layer that sits on top of the TTS `gateway`
and engine microservices. It is deliberately **dependency-light**
(pydantic + Python stdlib): no models, no GPU, and no network are needed to parse
a book, build the intermediate artifact, or run the tests. Only the *render* step
talks to a running engine.

---

## The big idea: one intermediate artifact

Everything reads and writes a single normalized JSON document — the **Book
Script** ([`schema.py`](schema.py)). It decouples *understanding* the book from
*rendering* it, and after rendering it doubles as the **player manifest**.

The rule that makes this work: keep **intent** (what a passage feels like —
engine-agnostic) separate from **render** (the concrete engine parameters).

```
BookScript
├── meta            title, author, language, source
├── cast[]          character  → voice_id (+ engine)
├── chapters[]      index, title, scenes[], audio_file, duration_ms
└── utterances[]    the flat, ordered spine of the whole book
        ├── speaker, kind            (narration | dialogue | internal | letter)
        ├── text                     one sentence, normalized
        ├── intent { emotion, intensity, pace }     ← what an analyzer decides
        ├── render { engine, voice_id, speed,       ← how it's actually synthesized
        │            lead_pause_ms, trail_pause_ms }
        └── audio  { start_ms, end_ms, sample_rate, cache_key }   ← filled by render
```

Why flat utterances? Rendering and the player timeline both iterate linearly; the
chapter/scene tree is navigation metadata layered on top.

---

## The flow

```
 book.txt ──▶ ingest ──▶ chunk ──▶ build ──▶ BookScript ──▶ render ──▶ output/
             (chapters,  (sentences, (assemble   (JSON       (Kokoro     chapter_NNN.wav
              scenes)     pauses)     utterances)  artifact)   + stitch)   manifest.json
                                                                          player.html
```

| Stage | Module | What it does |
|-------|--------|--------------|
| Ingest | [`ingest.py`](ingest.py) | `.txt`/markdown → chapters + paragraphs + scene breaks (line breaks *and indentation* preserved for verse). Heading guard stops prose like *"Chapter one of my life…"* from being read as a header. Marks leading **front matter** (copyright/dedication/contents) as `include_in_audio: false` and trailing **back matter** (about the author, acknowledgements) as `back_matter`, so a copyright page is never narrated as "Chapter 1". |
| Classify | [`classify.py`](classify.py) → [`profiles.py`](profiles.py) | Detects book type (heuristic, or optional LLM) → picks a **production profile** (voice, pace, pauses, chunk mode, skip-rules). |
| Clean | [`clean.py`](clean.py) | Applies the profile's skip-rules: drop page numbers / figure captions, strip citations (nonfiction/technical). A footnote marker glued to a word or a full stop (`lie.[1]`, `Somme¹`) is stripped under **every** profile — a marker is never speech. |
| Chunk | [`chunk.py`](chunk.py) | Prose: sentence splitting, guarding abbreviations (`Dr.`, `Ch.`, `pp.`, `vol.`), decimals, bare enumerators (`1.` at the head of a footnote body), and boundaries inside parentheses or quotations. Verse: one line per utterance. |
| Attribute + cast | [`attribute.py`](attribute.py) → [`cast.py`](cast.py) | Fiction only: split narration vs. dialogue, attribute each quote to a character **with a basis and a confidence** (abstaining to `Unknown` under 0.5), reset speaker state at every chapter boundary, map characters → distinct (gendered) Kokoro voices. |
| Build | [`build.py`](build.py) | Assembles the Book Script; voice / speed / pauses / chunking / casting all driven by the profile. Speaks the chapter heading when `read_headings` is on, closes the gap around a dialogue tag to 80 ms, and treats an indented short-line block as verse in any genre. |
| Render | [`render.py`](render.py) | Synthesizes each utterance via the gateway, caches by content hash, inserts pause silence, stitches per-chapter WAVs, and stamps timestamps back onto the artifact. |
| Generate | [`generate.py`](generate.py) | One command that runs the whole flow and writes a playable folder. |
| Player | [`player/player.html`](player/player.html) | Self-contained read-along web player. |

### Pause budget (in `chunk.py`, tuneable)

| Boundary | Silence |
|----------|---------|
| Between sentences | 200 ms |
| Around a dialogue tag (`"…," she said.`) | 80 ms |
| End of paragraph | 600 ms |
| After a spoken chapter heading | 900 ms |
| Scene break (`* * *`) | 1200 ms |
| End of chapter | 2000 ms |

A dialogue tag is one breath group with its quote — a narrator reads straight
through the comma — so the tag gap is a voice change, not a beat.

### Caching (why re-runs are cheap)

Each utterance is keyed by `sha256(schema_version + engine + voice + speed + text)`
and cached as a WAV under `output/<book>/.cache/`. Re-running only synthesizes
what changed — recast one character's voice and just their lines re-render.

---

## The Kokoro constraint (important)

Kokoro is the CPU baseline and needs no reference clip, but it exposes only
**named voices + speed** — no per-line emotion knob, no cloning. So in this
pipeline:

- **Character voices** = a deterministic `character → voice_id` map (the cast
  sheet). Consistent for free; you pick from Kokoro's built-in palette.
- **Emotion** is *not* faked with a dial that doesn't exist — it's steered through
  the levers Kokoro *does* have: **voice choice, pacing and pauses, and
  respelling through the pronunciation lexicon**. The `intent` field still records the analyzed
  emotion so a future expressive engine (StyleTTS2 / IndexTTS2, per utterance)
  can use it *without re-running analysis* — that's the whole point of keeping
  `intent` and `render` separate.
- **Pace is now steerable by the director.** A director pass returns a `pace`
  (0.8–1.2) per scene alongside its written note. Applying the pass writes
  `intent.emotion` / `intensity` / `pace` on **every** line whatever engine it is
  routed to, and on a line whose engine takes no written note — Kokoro, every
  line of it — that pace becomes real `render.speed`
  (`profile.base_speed × pace`, clamped 0.7–1.3). So a pass on a Kokoro-only book
  is audible instead of inert: it used to direct **zero** lines and report
  success at every step. The written `instruction` is still only stamped on
  instruction-capable engines (`direct.instruction_is_inert` is the same rule,
  exposed so the UI can warn); a pace is not a note, it is a number every engine
  honours. The multiplier always applies to the profile's base speed, never to
  the current speed, so re-applying a pass never compounds.

---

## Genre-aware production profiles

A book's *type* changes how it should sound, so a **production profile** is chosen
per book and drives every downstream stage. Classification is a heuristic by
default (structural fingerprints — verse lines, speaker cues, citations,
second-person + numbered steps, dialogue); pass `--llm-classify` to use
Ollama/Gemini instead (falls back to the heuristic on any failure). Force it with
`--book-type <type>`.

| Book type | Voice | Pace | Chunking | Strips |
|---|---|---|---|---|
| general_fiction | af_heart | 1.00 | prose | — |
| literary | af_heart | 0.96 | prose | footnote markers |
| fantasy_scifi | af_heart | 1.00 | prose | — |
| nonfiction | am_michael | 0.97 | prose | page #s, citations, footnotes, figure captions |
| selfhelp | af_heart | 1.00 | prose | page #s |
| poetry | af_heart | 0.90 | **verse** (line-preserving) | — |
| drama | af_heart | 1.00 | prose | — |
| children | af_heart | 0.92 | prose | — |
| technical | am_michael | 0.97 | prose | page #s, citations, footnotes, figure captions |

The chosen profile is stored in the Book Script (`profile` + `meta.book_type`), so
it's inspectable and overridable. `multi_voice` (on for fiction) drives speaker
attribution + casting — see below. The engine-agnostic `intent` field is
untouched by profiles — that's where a future expressive engine reads per-line
emotion.

---

## Multi-voice: speaker attribution + casting

For `multi_voice` profiles (fiction), prose is split into narration vs. dialogue,
each quote is attributed to a character, and characters are cast to distinct
Kokoro voices. Attribution is heuristic ([`attribute.py`](attribute.py)):
`"…" said X` / `X said, "…"`, pronoun resolution (`"…" she called` → the recent
female name), same-speaker continuation within a paragraph, and back-and-forth
alternation across paragraphs.

**It says how sure it is.** Every line carries
`attribution: {basis, confidence}` — `tag` 0.95, `continuation` 0.7, `pronoun`
0.6, `alternation` 0.4 — and anything under 0.5 becomes `Unknown`, which still
gets its own voice. Alternation is deliberately under the bar: it used to answer
with the same confidence as an explicit tag, and a wrong speaker is invisible in
the finished audio while an `Unknown` is a work item you can see and fix. Speaker
state also **resets at every chapter boundary**, so the first quote of a chapter
can no longer inherit whoever spoke last in the previous one; learned gender
survives the reset, because that is a property of the character rather than of
where you are in the book. Casting ([`cast.py`](cast.py)) is gender-aware and deterministic
(same book → same cast → cache hits), drawing from Kokoro's real voice set;
narration always keeps the profile's narrator voice. Override with
`--single-voice` / `--multi-voice`.

It won't get every line right (full coreference is a research problem) — the
accuracy upgrade is an optional LLM attribution pass, which the cast structure
already supports.

---

## Usage

Run from the repo root.

```bash
# Inspect what parsing produced (no audio, no gateway):
python -m pipeline.build pipeline/samples/sample.txt --stats

# Full offline dry-run (fake synth — proves the plumbing without a gateway):
python -m pipeline.generate pipeline/samples/sample.txt \
    --title "The Cold Lantern" --out output/cold-lantern --offline

# Real audio via a running gateway (Kokoro engine up, gateway on :8080):
python -m pipeline.generate pipeline/samples/sample.txt \
    --title "The Cold Lantern" --voice af_heart --out output/cold-lantern

# Play it:
cd output/cold-lantern && python -m http.server 8100
# open http://localhost:8100/player.html
```

Config: `--gateway` (or `AUDIOBOOK_GATEWAY` env) points at the gateway;
`--voice` picks the Kokoro narrator voice; `--offline` uses the fake synth;
`--no-normalize` skips peak normalization.

### The player

`player.html` fetches `manifest.json` + the chapter WAVs and gives you:
read-along transcript with the current sentence highlighted, play/pause, ±15 s,
prev/next chapter, playback speed, click-a-line-to-seek, bookmarks
(localStorage; right-click a bookmark to delete), and resume-where-you-left-off.
It must be served over HTTP (browsers block `fetch`/audio over `file://`).

---

## Tests

```bash
pytest pipeline/tests    # fully offline (fake synth), no models/GPU
```

Covers: sentence splitting (abbreviations, decimals, dialogue), chapter/scene
detection + the heading guard, pause assignment, JSON round-trip, render
timestamps, caching, and the generated output folder.

---

## The Studio service (`pipeline/studio/`)

The CLI above renders a book once. The **studio** is the same pipeline as a
persistent workspace — the thing the Director's Desk UI talks to.
[`docs/STUDIO-API.md`](../docs/STUDIO-API.md) is the contract: every endpoint,
every shape, and the seven semantics that must hold. Read it before changing
anything here.

```
pipeline/studio/
  store.py   books on disk: script versions, lexicon, settings, derived state
  jobs.py    one worker thread, persisted job records, SSE pub/sub
  api.py     the FastAPI routers
  app.py     create_app() — /api/… and the book files under /books/…
```

A book lives in `output/<slug>/`: `book.json`, the verbatim `source.*`, a
versioned `script.json` (+ `versions/script.vNNNN.json`), the rendered
`manifest.json` and `chapter_NNN.wav`, `.cache/<key>.wav`, `passes/<id>.json`,
`jobs/<id>.json`, `exports/`, `lexicon.json`.

Three things are load-bearing:

- **`audio_state` is derived, never stored.** A line is `cached` when a file
  exists for the key it hashes to *right now*, `stale` when it was rendered
  under a different key, `missing` when it never was. Editing a note, a voice,
  an engine, the text, or a lexicon entry moves the key — there is no dirty flag
  to forget to clear. Anything asking "is this rendered?" must go through
  `Renderer.key_for`, which applies the same `text_transform` the renderer will.
- **Job records are files.** Written atomically after every progress step, and
  re-read at startup: a `running` job found on disk becomes `interrupted`, and
  `POST /jobs/{id}/resume` re-queues it. Pause and cancel are cooperative flags
  polled between lines and scenes, never a killed thread — a half-written cache
  entry would hash as a perfectly valid take.
- **Offline runs the same code.** `STUDIO_OFFLINE=1` swaps the synth callable
  and `STUDIO_FAKE_DIRECTOR=1` swaps the annotate callable; the job kinds, the
  pass shapes and the cache keys are the real ones. That is how the whole UI is
  verified without a GPU or an LLM.

Run it: `docker compose up -d studio` (API + `/docs` on :5174, UI on :5173), or
`uvicorn pipeline.webapp:app --port 5174` from the repo root.

## Status & roadmap

**Done.** Ingest `.txt`/`.md`/**`.epub`** → classify → production profile → clean
(skip-rules) → attribute speakers + cast voices → Book Script → render + cache →
per-chapter WAV + timestamped manifest → read-along player. On top of that:

- **Book-scale director pass** (`direct.py`) — scene-level, cached, resumable,
  reviewable. A pass is a document of proposals; nothing reaches the book until
  someone accepts it, and an annotated passage is only applied if stripping the
  tags reproduces the source exactly.
- **Async jobs** (`studio/jobs.py`) — render / direct / export with progress,
  a live event stream, pause · resume · cancel, and survival across a restart.
- **Hybrid casting** (`cast.py`) — narrator on Kokoro, characters on the
  expressive engine, per character, stable across chapters. Any book that routes
  a line through Breeze carries a non-commercial licence note.
- **Pronunciation lexicon** (`lexicon.py`) — per-book and global respellings,
  applied before the cache key, so an edit re-renders exactly the lines it
  touches.
- **Mastering + export** (`master.py`) — two-pass EBU R128 loudnorm to an
  ACX-ish window, a true-peak limiter, 44.1 kHz, chaptered `.m4b` / `.mp3` /
  `.wav`, plus `--format acx`: one 192 kbps CBR MP3 per included chapter with
  0.75 s of generated room tone at each end, a split before the 120-minute
  ceiling at a real utterance boundary, and a **measured compliance table** per
  file (integrated LUFS, true peak, room-tone level, duration).
- **The Studio API + Director's Desk UI** — see above.

**Not built, on purpose:** waveform/timeline editor · an
eighth engine · per-line LLM direction · streaming synthesis · LLM speaker
attribution (measure the heuristic first) · accounts.

**Next:**
1. **PDF ingestion** — de-hyphenation + header/footer stripping, behind the same
   `RawBook` interface.
2. **LLM speaker attribution** — the heuristic nails common patterns; measure it
   before replacing it.
3. **Parallel synth** — the job worker is deliberately serial (one GPU); a CPU
   engine like Kokoro could render several lines at once.
4. **Cover art + richer metadata** on the export.
