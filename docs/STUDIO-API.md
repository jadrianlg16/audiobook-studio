# Studio API — the contract between the UI and the studio service

Status: v1 contract, written 2026-09-05 before implementation. The frontend is
built against this document (with a mock adapter); the studio service implements
it. Any change goes here first.

## Topology

```
browser ──► frontend (nginx, :5173)
              ├── /            React SPA (shelf, desk, review, job, lab)
              ├── /api/…       → gateway:8000/api/…     (engines, synth, bake-off, director)
              └── /studio/…    → studio:8000/…          (books, jobs, passes, exports, static books)
```

- The UI calls the **gateway** at `/api/...` (unchanged) and the **studio** at
  `/studio/api/...`. Book files (player, chapter WAVs, exports) are at
  `/studio/books/<slug>/...`.
- Direct access to the studio service (`:5174`) exposes the same paths without
  the `/studio` prefix: `/api/...` and `/books/...`.
- All JSON. Errors: `{ "detail": "<human message>" }` with 4xx/5xx.

## Book folder layout (`output/<slug>/`)

```
book.json              slug, title, author, created_at, updated_at, source_kind, settings
source.txt|source.epub the ingested source, verbatim
script.json            the working Book Script (edits go here)
versions/script.vNNNN.json   one per save (append-only)
manifest.json          the last *rendered* script with timings — what the player reads
chapter_NNN.wav        rendered chapters
player.html            read-along player (existing)
.cache/<key>.wav       per-utterance render cache (content-hash keyed)
.cache-offline/        the same, for STUDIO_OFFLINE=1 renders — fake clips never pose as real audio
passes/<pass_id>.json  director passes
jobs/<job_id>.json     job records (persisted so a restart can resume)
exports/<file>         mastered exports (.m4b / .wav / .mp3)
lexicon.json           per-book pronunciation lexicon
```

Global: `output/settings.json` (studio settings), `output/lexicon.json` (global lexicon).

## Types

```ts
type Slug = string;                       // [a-z0-9-]+
type AudioState = "cached" | "stale" | "missing";
// cached  = a cache file exists for the line's CURRENT key
// stale   = the line was rendered before but its key changed (text/direction/voice/engine edited)
// missing = never rendered
type DirectionSource = "none" | "director" | "manual";

interface BookSummary {
  slug: Slug; title: string; author: string;
  book_type: string;                      // profile id, e.g. "general_fiction"
  profile_label: string;                  // e.g. "General fiction"
  language: string;
  chapters: number; utterances: number; words: number;
  est_minutes: number;                    // words / 155
  duration_ms: number;                    // from manifest (0 if never rendered)
  engines: Record<string, number>;        // engine_id -> utterance count (from script)
  directed: { directed: number; total: number };   // utterances with render.instruction set
  audio: { cached: number; stale: number; missing: number };
  cast: CastEntry[];
  created_at: string; updated_at: string; rendered_at: string | null;   // ISO 8601
  last_job: JobSummary | null;
  status: "idle" | "rendering" | "directing" | "exporting" | "failed";
  licence_note: string | null;            // set when any engine in use is non-commercial (Breeze)
  exportable: boolean;                    // every chapter is fresh (what POST /export requires)
}

interface CastEntry { character: string; voice_id: string | null; engine: string; notes: string; voice_design: string | null; }

interface Line {                          // = Utterance + derived state
  id: string; order: number; chapter_index: number; scene_index: number;
  speaker: string; kind: "narration"|"dialogue"|"internal"|"letter"|"heading"|"verse";
  text: string;                           // what the reader sees (never contains tags)
  intent: { emotion: string; intensity: number; pace: number };
  render: {
    engine: string; voice_id: string | null; speed: number;
    lead_pause_ms: number; trail_pause_ms: number;
    instruction: string | null;           // the performance note
    spoken_text: string | null;           // text with inline tags, for tag-capable engines
    direction_source: DirectionSource;
    voice_design: string | null;          // per-character voice description (engines without named voices)
  };
  audio: { start_ms: number; end_ms: number; sample_rate: number; cache_key: string } | null;
  audio_state: AudioState;
  duration_ms: number | null;             // from the cache file if cached, else last render, else null
}

interface Scene { id: string; chapter_index: number; scene_index: number; line_ids: string[]; }
// scene id format: "c{chapter_index}_s{scene_index}"

interface Chapter {
  index: number; title: string; scenes: Scene[];
  audio_file: string | null; duration_ms: number;
  lines: number; directed: number; audio: { cached: number; stale: number; missing: number };
  fresh: boolean;                         // chapter WAV reflects every line's current key
}

interface Script {                        // GET /api/books/{slug}/script
  book: BookSummary;
  version: number;                        // increments on every save
  profile: object;                        // ProductionProfile as stored
  cast: CastEntry[];
  chapters: Chapter[];
  lines: Line[];                          // flat, ordered
}

interface JobSummary {
  id: string; kind: "render" | "direct" | "export"; book: Slug;
  status: "queued" | "running" | "paused" | "done" | "failed" | "cancelled" | "interrupted";
  // interrupted = the service restarted mid-job; POST /resume re-queues it (cache makes it cheap)
  created_at: string; started_at: string | null; finished_at: string | null;
  progress: { done: number; total: number; cache_hits: number; synth_calls: number;
              rtf: number | null; elapsed_s: number; eta_s: number | null };
  error: string | null;
  result: object | null;                  // kind-specific (see below)
}

interface Job extends JobSummary {
  chapters: { index: number; title: string; done: number; total: number;
              status: "queued"|"running"|"done"|"failed"|"skipped";
              seconds: number; engines: Record<string, number> }[];
  log: { ts: string; line: string }[];    // last 200 entries
  request: object;                        // the body that started it
}
// result by kind:
//   render: { chapters_written: number[], duration_ms: number }
//   direct: { pass_id: string, counts: PassCounts }
//   export: { file: string, url: string, bytes: number, format: string, loudness: string }

interface PassCounts { kept: number; rejected: number; ignored: number; error: number; scenes: number; }

interface PassScene {                     // + reason, prompt_version (Amendment v1.4)
  scene_id: string; chapter_index: number; scene_index: number; chapter_title: string;
  status: "kept" | "rejected" | "ignored" | "error";
  // kept     = an instruction and/or a tag-annotated text was accepted
  // rejected = the model's annotated text was NOT the source text (paraphrase / truncation)
  // ignored  = the model returned nothing usable for the target engine
  // error    = the LLM call failed (see `error`)
  instruction: string | null;             // proposed scene-level note (validated, ≤ 400 chars)
  tags_status: "kept" | "rejected" | "none";
  tags_used: Record<string, number>;      // e.g. { "(sigh)": 1 }
  source_text: string;                    // the scene passage sent to the model
  model_text: string | null;              // the model's annotated passage (kept OR rejected — shown in the diff)
  line_spoken_text: Record<string, string> | null;  // utterance_id -> spoken_text, only when tags kept
  summary: string; emotion: string; scalar: number; // the director's generic reading of the scene
  decision: "pending" | "accepted" | "edited" | "rejected" | "kept_text";
  // kept_text = accept the instruction but keep the book's text (no tags) — the fix for a rejected scene
  final_instruction: string | null;       // what was applied (after edit)
  attempts: number; error: string | null;
}

interface Pass {
  id: string; book: Slug; created_at: string; finished_at: string | null;
  provider: "ollama" | "gemini"; model: string; prompt_version: string;
  target_engine: string;                  // the engine the notes are written for (e.g. "breeze")
  scope: { scenes: string[] };
  counts: PassCounts; elapsed_s: number;
  scenes: PassScene[];
  applied_at: string | null;              // set once decisions are committed to the script
}

interface Settings {
  offline: boolean;                       // STUDIO_OFFLINE=1 → fake synth (no gateway)
  fake_director: boolean;                 // STUDIO_FAKE_DIRECTOR=1 → canned direction (no LLM)
  gateway_url: string;
  narrator_engine: string;                // default "kokoro"
  expressive_engine: string;              // default "breeze"
  casting_mode: "narrator_only" | "dialogue_expressive" | "all_expressive";
  director: { provider: "ollama" | "gemini"; model: string };
  export: { format: "m4b" | "mp3" | "wav"; loudness: "acx" | "none"; sample_rate: number };
                                          // + format "acx" and bitrate (v1.1; spelled out in v1.4)
  ffmpeg: boolean;                        // ffmpeg available in this deployment
  book_types: string[];                   // profile ids for the New book form ("auto" is implicit)
  engines: { id: string; name: string; available: boolean; instruction: boolean; audio_tags: string[];
             voices: { id: string; label: string }[]; license: string; non_commercial: boolean }[];
             // a cached copy of the gateway's /api/engines, flattened for the UI
}
```

## Endpoints (studio, prefix `/api`)

### Books

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/books` | – | `BookSummary[]` (newest updated first) |
| POST | `/books` | `{ title, author, text, book_type: "auto"|<type>, voices: "auto"|"single"|"multi", casting_mode? }` | `BookSummary` (201). Builds the script, does NOT render. Slug from title; collision → `-2`, `-3`. |
| POST | `/books/upload` | multipart: `file` (.txt/.md/.epub), `title?`, `author?`, `book_type?`, `voices?`, `casting_mode?` | `BookSummary` (201). EPUB metadata fills title/author when blank. A blank or absent `casting_mode` means the settings' default, as on `POST /books`. |
| GET | `/books/{slug}` | – | `BookSummary` |
| DELETE | `/books/{slug}` | – | 204. Removes the folder. |
| GET | `/books/{slug}/script` | – | `Script` |
| GET | `/books/{slug}/versions` | – | `{ version: number, saved_at: string, note: string }[]` |
| POST | `/books/{slug}/versions/{n}/restore` | – | `Script` (a new version equal to `n`) |
| PATCH | `/books/{slug}/lines/{id}` | any of `{ instruction, spoken_text, engine, voice_id, speed, text }` (`null` clears) | `{ line: Line, version }`. Sets `direction_source: "manual"` when instruction/spoken_text change; clearing both sets it back to `"none"` (the next applied pass may direct the line again). Editing `text` clears `spoken_text`. |
| POST | `/books/{slug}/lines/{id}/render` | `{}` | `{ line: Line, audio_url }` — synchronous single-line render (the "hear it back in seconds" loop). 503 if a render job is running. |
| GET | `/books/{slug}/lines/{id}/audio` | – | `audio/wav` for the line's current key; 404 if missing. |
| PATCH | `/books/{slug}/cast` | `CastEntry[]` (partial OK: match on `character`) | `Script`. Re-stamps voice/engine on that character's lines. |
| POST | `/books/{slug}/casting` | `{ mode: casting_mode, expressive_engine? }` | `Script`. Applies the hybrid casting rule to the whole cast. |
| GET | `/books/{slug}/lexicon` | – | `{ entries: Record<string,string>, global: Record<string,string> }` |
| PUT | `/books/{slug}/lexicon` | `{ entries: Record<string,string> }` | same. Lines whose text contains a changed key become `stale`. |

### Rendering (async)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/books/{slug}/render` | `{ scope: "all"|"stale"|"chapters"|"lines", chapters?: number[], line_ids?: string[] }` | `Job` (202). One job runs at a time; others queue. Every scope uses the cache; `all` re-stitches every chapter, `stale` only chapters with a non-cached line. |
| POST | `/books/{slug}/direct` | `{ scope: "all"|"undirected"|"chapters"|"scenes", chapters?: number[], scenes?: string[], provider?, model?, target_engine? }` | `Job` (202) of kind `direct`; result carries `pass_id`. |
| POST | `/books/{slug}/export` | `{ format?, loudness?, sample_rate? }` (defaults from settings) | `Job` (202) of kind `export`. 409 if any chapter is not fresh. |
| GET | `/books/{slug}/exports` | – | `{ file, url, bytes, created_at, format, loudness, sample_rate }[]` |

### Director passes

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/books/{slug}/passes` | – | `Pass[]` without `scenes` (summary) |
| GET | `/books/{slug}/passes/{pass_id}` | – | `Pass` |
| POST | `/books/{slug}/passes/{pass_id}/decide` | `{ decisions: { scene_id: string, decision: "accepted"|"edited"|"rejected"|"kept_text", instruction?: string }[] }` | `Pass`. Only records decisions. |
| POST | `/books/{slug}/passes/{pass_id}/apply` | `{}` | `{ pass: Pass, script: Script }`. Writes accepted/edited/kept_text scenes into the script (`direction_source: "director"`, or `"manual"` for edited), saves a version, sets `applied_at`. Affected lines become `stale`. Idempotent. |
| POST | `/books/{slug}/passes/{pass_id}/retry` | `{ scenes: string[] }` | `Job` (202) — re-runs the director for those scenes inside the same pass (`attempts` increments, decision resets to `pending`). |

### Jobs

| Method | Path | Returns |
|---|---|---|
| GET | `/jobs` | `JobSummary[]` (newest first, all books) |
| GET | `/jobs/{id}` | `Job` |
| GET | `/jobs/{id}/events` | `text/event-stream`. Events: `progress` (data = `Job` minus `log`), `log` (data = one log entry), `done` (data = `Job`). Sends a `progress` immediately on connect. Heartbeat comment every 15 s. |
| POST | `/jobs/{id}/pause` · `/resume` · `/cancel` | `Job`. Pause/cancel are cooperative (take effect between lines). `resume` also re-queues an `interrupted` or `failed` job. |

### Settings & meta

| Method | Path | Returns |
|---|---|---|
| GET | `/settings` | `Settings` |
| PATCH | `/settings` | `Settings` (persists `narrator_engine`, `expressive_engine`, `casting_mode`, `director`, `export`) |
| GET | `/health` | `{ status: "ok", gateway: "ok"|"unreachable", offline, fake_director }` |

### Gateway additions (prefix `/api`, served by the gateway)

| Method | Path | Returns |
|---|---|---|
| GET | `/director/models` | `{ provider: "ollama", models: { name: string, size_bytes: number, modified_at: string }[], default: string, error: string | null }` — proxies Ollama `GET /api/tags`; `error` set (and `models: []`) when Ollama is unreachable. |
| GET | `/director/defaults` | existing: `{ provider, model }` |
| POST | `/director/annotate` | existing; each `per_engine[]` entry gains `rejected_text: string | null` (the model's annotated passage when it failed validation) and `text_status: "kept" | "rejected" | "none"`. |

## Semantics that must hold

1. **The cache key is the interface.** `audio_state` is derived from the line's
   current render key (`Renderer.cache_key`) vs. the cache directory. Editing a
   line's `instruction`, `spoken_text`, `text`, `voice_id`, `engine`, or `speed`
   flips exactly that line to `stale`. A render job after editing four notes must
   report a cache-hit rate near 100%.
2. **`text` is what the reader sees; `spoken_text` is what the engine hears.**
   Tags never appear in `text`. `spoken_text` is only ever sent to an engine whose
   `audio_tags` include every tag it contains; otherwise the studio sends `text`.
3. **A director pass never edits the book.** A scene's annotated text is applied
   only if stripping the tags reproduces the source exactly (the gateway's rule).
   Rejections are kept, with the model's text, so the review screen can show the
   diff.
4. **Scene-level direction, line-level override.** The director writes one note
   per scene; applying it stamps every line in the scene that is routed to an
   instruction-capable engine. A manual edit on one line (`direction_source:
   "manual"`) is never overwritten by a later pass unless the user re-applies.
5. **Jobs survive restarts.** Job records are files; on startup any `running`
   job becomes `interrupted`. Resume re-queues; the cache makes it cheap.
6. **Offline and fake-director modes exercise the same code paths** as the real
   ones (same job kinds, same pass shapes) so the UI can be verified end-to-end
   without a model.
7. **Licence surfacing.** Any book with a line routed to a non-commercial engine
   (Breeze) carries `licence_note`; the UI shows it on the shelf row and the desk.

## Amendments v1.1 (2026-09-05)

Additive unless marked BREAKING.

### Book summary

```ts
interface BookSummary {
  // ...everything above, plus:
  directable: { lines: number; total: number };   // lines routed to an engine that declares `instruction`
  needs_render: { lines: number; chapters: number }; // lines not cached + chapters whose WAV is not fresh
  synthetic: boolean;                              // the last render used the fake synth (STUDIO_OFFLINE=1): test audio, not speech
}
```

- `POST /books/{slug}/render` with `scope: "stale"` now also re-stitches every chapter that is
  not fresh, even when every line is cached (the state a single-line re-render leaves behind).
  The UI labels the action "Render N stale lines" when `needs_render.lines > 0`, else
  "Rebuild N chapters" when `needs_render.chapters > 0`, else disabled.
- `POST /books/{slug}/direct` returns **409** when `directable.lines == 0` (detail explains that
  no line in the book is routed to an engine that takes a written note). `target_engine`
  defaults to the instruction-capable engine used by most lines in the book, then to
  `settings.expressive_engine`.

### Lines

```ts
interface Line {
  // ...plus:
  flag: boolean;                 // QC pickup marker
  note: string;                  // free-text QC note (never sent to an engine)
  attribution: { basis: "tag" | "pronoun" | "continuation" | "alternation" | "manual" | "none";
                 confidence: number };   // 0..1; heuristic abstains to speaker "Unknown" below 0.5
}
```

- `PATCH /books/{slug}/lines/{id}` additionally accepts `speaker` (reassign the line to an
  existing character, or a new one — a cast entry is created with a voice/engine chosen by the
  casting rule; the line takes that entry's voice/engine and becomes stale), `flag`, `note`.
  Setting `speaker` sets `attribution.basis = "manual"`, `confidence = 1`.
- `GET /books/{slug}/script?filter=stale|undirected|flagged|unknown&speaker=<name>` is optional
  server-side filtering; the UI may filter client-side instead.

### Chapters

```ts
interface Chapter {
  // ...plus:
  kind: "chapter" | "front_matter" | "back_matter";
  include_in_audio: boolean;     // false = skipped by render/export; shown as "skipped" in the rail
}
```

- `PATCH /books/{slug}/chapters/{index}` body `{ title?, kind?, include_in_audio? }` → `Chapter`.
- Ingest marks an untitled leading chapter that reads like a copyright/dedication page as
  `front_matter` with `include_in_audio: false`. Headings are spoken as their own line
  (`kind: "heading"`) when the profile's `read_headings` is on.

### Cast

- `POST /books/{slug}/cast/merge` body `{ from: string, into: string }` → `Script`. Every line
  of `from` becomes `into`; `from` leaves the cast.
- `GET /books/{slug}/cast` → `{ cast: CastEntry[], lines: Record<character, number>,
  first_line: Record<character, string>, shared_voices: string[][] }`.

### Lexicon

- `GET /books/{slug}/lexicon` additionally returns `occurrences: Record<term, number>` (lines
  whose text contains the term) so the editor can show which entries do anything.

### Director pass

```ts
interface PassScene {
  // ...plus:
  scene_note: string | null;     // context for the human ("an exchange between Elara and Marcus…"); never sent to an engine
  pace: number | null;           // 0.8..1.2 relative reading speed the director asked for
}
```

- `apply` writes `intent.emotion` / `intent.intensity` / `intent.pace` on every line of an
  accepted scene **regardless of engine**, and for lines whose engine does not take a written
  note it applies `pace` to `render.speed` (`profile.base_speed × pace`, clamped 0.7..1.3) so a
  pass has an audible effect on Kokoro. `instruction` (the line note) is still only stamped on
  instruction-capable engines. UNVERIFIED with a real LLM until models run; the fake director
  emits a pace.

### Export

- `format: "acx"` produces one MP3 per included chapter: 192 kbps CBR, 44.1 kHz, 0.75 s of
  room tone (very low noise, not digital silence) at head and tail, two-pass loudnorm then a
  true-peak limiter at −3.5 dBTP, chapters longer than 115 min split at a scene boundary.
  Result: `{ files: [{ file, url, bytes, chapter, duration_ms, measured: {integrated_lufs,
  true_peak_dbtp}, checks: { peak_ok, loudness_ok, duration_ok } }], compliance: "pass" | "fail",
  format: "acx" }`. `GET /exports` lists them. `bitrate` on the request is honoured.

### Settings

- `engines[]` is the source of truth for the UI (never the gateway's `/api/engines`, which
  returns `info: null` for an engine that is not running). `available` there still reflects the
  gateway's view.

### Service-side clarifications (implemented 2026-09-05)

- `POST /books`: `title` is required, non-blank, ≤ 200 chars (slug capped at 60 chars for
  Windows paths); `author` is optional.
- Every error body is `{ "detail": "<string>" }`; FastAPI's 422 list is flattened to one
  string ("field: message; …").
- `PATCH /lines/{id}`: unknown keys → 422; `speed: null` resets to 1.0; `text: ""` → 422; the
  merged render is re-validated by the schema before anything is written, so a bad value can
  never brick a book. A stored script that fails validation returns 500 with a detail naming
  the file and pointing at `POST /books/{slug}/versions/{n}/restore`.
- `PATCH /cast`: a row for a character not in the cast is 404 unless it carries `create: true`.
- `Line.flag` / `Line.note` live in a per-book `qc.json` sidecar merged into `Line` views;
  setting them writes no new script version and does not touch the cache key.
- `GET /exports` returns one row per file; ACX rows carry `chapter`, `duration_ms`, `measured`,
  `checks`, `compliance`, `bitrate`. The grouped `{ files[], compliance, format }` is the
  export job's `result`. `Settings.export` gains `bitrate`.
- `DELETE /books/{slug}` cancels in-flight jobs (bounded wait) and removes their records from
  `GET /jobs`.
- `POST /render` / `POST /direct` with an explicit scope and empty or unknown ids → 422.
- `Settings.engines` merges the gateway's live answer over a static catalog
  (`pipeline/studio/engines.py`) so the list is never empty; `available` is the gateway's word.
- `render_mode` (`"offline" | "live"`) is stamped on `book.json` by every render;
  `synthetic = render_mode == "offline"`; books rendered before the stamp are inferred from
  which cache directory exists (`unknown` → `synthetic: false`).
- Not implemented (optional): `GET /script?filter=…&speaker=…`.
- CLI note: `python -m pipeline.direct` takes the `manifest.json` that `pipeline.generate`
  writes (there is no separate `script.json` on the CLI path).

### Amendment v1.2 (2026-09-05, chapter sign-off)

```ts
interface Chapter {
  // ...plus:
  qc_status: "unheard" | "approved" | "pickups";   // a person's verdict on the chapter's audio; default "unheard"
}
```

- `PATCH /books/{slug}/chapters/{index}` additionally accepts `qc_status`. Stored in the
  per-book `qc.json` sidecar (like line flags); never touches the cache key or the script
  version. `BookSummary.qc` = `{ approved: number, pickups: number, chapters: number }`
  (included chapters only). Any render that changes a chapter's audio resets that chapter
  to `"unheard"`.
- `GET /jobs` is what the UI's Jobs page lists (newest first, all books).

### Amendment v1.3 (2026-09-26, OpenAPI export)

No shape changes. The six JSON routes that answered an untyped object now declare
their response model, so the service's OpenAPI names every JSON body:

| Route | Model (OpenAPI component) | Shape, as in the tables above |
|---|---|---|
| `PATCH /books/{slug}/lines/{id}` | `LinePatchResult` | `{ line: Line, version }` |
| `POST /books/{slug}/lines/{id}/render` | `LineRenderResult` | `{ line: Line, audio_url }` |
| `GET /books/{slug}/passes` | `PassSummary[]` | `Pass` without `scenes` |
| `GET /books/{slug}/passes/{pass_id}` | `Pass` | `Pass` |
| `POST /books/{slug}/passes/{pass_id}/decide` | `Pass` | `Pass` |
| `POST /books/{slug}/passes/{pass_id}/apply` | `PassApplyResult` | `{ pass: Pass, script: Script }` |

- **`docs/studio-openapi.json`** is the machine-readable export of the studio's OpenAPI —
  paths as the service serves them (`/api/…`; nginx strips `/studio`), sorted keys, LF.
  Regenerate with `python scripts/export_studio_openapi.py` after changing a route or a
  wire model; `--check` exits 1 when the committed file is stale, and
  `pipeline/tests/test_openapi_export.py` fails on the same drift. This document stays the
  contract for semantics; the export is what the UI generates its types from.

### Amendment v1.4 (2026-09-26, the export describes the wire exactly)

No payload changes: every body the service sends is byte-for-byte what it sent before.
What changes is how exactly this document and `docs/studio-openapi.json` describe it, so
the UI can type-check against the export instead of trusting a hand-written mirror.

**Presence.** Every field of every response type in this document is always present on
the wire. `| null` means "present, may be null"; nothing in a response is ever omitted.
In the export, response schemas mark every field `required` (Pydantic's
`json_schema_serialization_defaults_required`). A model used both in a request body and
inside a response is exported twice: `<Name>-Input` (fields with a default are optional)
and `<Name>-Output` (every field required) — today `DirectorSettings` and
`ExportSettings`. (`BookSummary.qc` and `Chapter.qc_status` are always sent; a client may
still treat them as optional, as the v1.2 note allows.)

**Closed sets are closed on the wire.** These were plain strings in the service:

```ts
type LineKind = "narration" | "dialogue" | "internal" | "letter" | "heading" | "verse";
interface Line {
  kind: LineKind;
  render: { direction_source: DirectionSource; /* …as above */ };
  attribution: { basis: "tag" | "pronoun" | "continuation" | "alternation" | "manual" | "none";
                 confidence: number };   // always an object — narration is { basis: "none", confidence: 1 }
}
```

**Corrections to the types above** (the wire already worked this way; the text did not say so):

```ts
interface Settings {
  export: { format: "m4b" | "mp3" | "wav" | "acx"; loudness: "acx" | "none";
            sample_rate: number; bitrate: string | null };   // v1.1 added "acx" and bitrate
  engines: { /* …as above… */ voices: { id: string; label: string }[] }[];
}
// PATCH /settings takes the same `export` object; `bitrate` is optional there.

interface ExportFile {                    // GET /books/{slug}/exports, one row per file
  file: string; url: string; bytes: number; created_at: string;
  format: string; loudness: string; sample_rate: number;
  // ACX rows fill these; every other row sends them as null:
  chapter: number | null; duration_ms: number | null; bitrate: string | null;
  measured: Record<string, number> | null;   // at least integrated_lufs, true_peak_dbtp on ACX rows
  checks: Record<string, boolean> | null;    // peak_ok, loudness_ok, duration_ok on ACX rows
  compliance: "pass" | "fail" | null;
}

interface PassScene {
  // ...plus two fields the service always sent:
  reason: string | null;       // why a scene was rejected or ignored, one sentence (`error` stays the failed call)
  prompt_version: string;      // the prompt that produced this scene; a resumed pass may mix prompts
}
```

**The export declares what the service really sends:**

- Errors: every operation declares a `default` response `ErrorDetail` = `{ detail: string }`
  — the only error body the service sends (v1.1: FastAPI's 422 list is flattened). The
  `HTTPValidationError` schema FastAPI would advertise is gone from the export.
- `PATCH /books/{slug}/lines/{id}` and `PATCH /books/{slug}/cast` parse their bodies by hand
  (to tell "absent" from `null` and word their own 422s). The export now names those bodies
  — `LinePatch` and `CastPatchEntry[]` — without changing how they are parsed.
- `GET /books/{slug}/lines/{id}/audio` is `audio/wav` (binary) and `GET /jobs/{id}/events`
  is `text/event-stream`, not JSON.

The UI generates `frontend/src/studio/api/schema.gen.ts` from the export (`npm run
gen:api`; `npm run check` fails when it is stale) and `frontend/src/studio/api/contract.ts`
fails `tsc` when a hand-written type in `types.ts` disagrees with it.
