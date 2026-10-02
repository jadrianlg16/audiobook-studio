// The studio's wire types, as the UI uses them.
//
// docs/STUDIO-API.md (v1 + Amendments v1.1–v1.4) is the contract; its
// machine-readable export, docs/studio-openapi.json, is generated into
// ./schema.gen.ts (`npm run gen:api`). Since v1.4 most types here are plain
// aliases of that schema. The few that are still written by hand narrow or
// widen the wire *on purpose*, and each says why; ./contract.ts fails `tsc`
// when any of them stops agreeing with the export.
//
// Anything not in the contract does not belong here.

import type { components } from "./schema.gen";

type Schemas = components["schemas"];

export type Slug = string;

// ---- closed sets, read off the export ----------------------------------------

export type AudioState = Schemas["Line"]["audio_state"];
export type DirectionSource = Schemas["Render"]["direction_source"];
export type CastingMode = Schemas["Settings"]["casting_mode"];
export type BookStatus = Schemas["BookSummary"]["status"];
export type JobKind = Schemas["JobSummary"]["kind"];
export type JobStatus = Schemas["JobSummary"]["status"];
export type SceneStatus = Schemas["PassScene"]["status"];
export type SceneDecision = Schemas["PassScene"]["decision"];
export type LineKind = Schemas["Line"]["kind"];
/** v1.1: how the attributor decided who speaks this line. */
export type AttributionBasis = Schemas["Attribution"]["basis"];
/** v1.1: front/back matter is rendered only when `include_in_audio`. */
export type ChapterKind = Schemas["Chapter"]["kind"];
/**
 * v1.2: a person's verdict on a chapter's audio. Never inferred — only a human
 * sets it, and any render that changes the chapter's audio resets it to
 * "unheard".
 */
export type QcStatus = Schemas["Chapter"]["qc_status"];

// ---- responses: exactly the wire -----------------------------------------------

export type CastEntry = Schemas["CastEntry"];
export type LineAudio = Schemas["AudioSpan"];
/** 0..1 `confidence`; the heuristic abstains to speaker "Unknown" below 0.5. */
export type Attribution = Schemas["Attribution"];
/** A script line: the pipeline's Utterance plus derived audio state (v1.1 QC fields). */
export type Line = Schemas["Line"];
export type Scene = Schemas["Scene"];
/** The counters inside a job record (`JobSummary.progress`; `JobProgress` on the wire). */
export type JobCounters = Schemas["JobProgress"];
export type JobChapter = Schemas["JobChapter"];
export type JobLogEntry = Schemas["JobLogEntry"];
export type PassCounts = Schemas["PassCounts"];
export type PassScene = Schemas["PassScene"];
export type Pass = Schemas["Pass"];
/** A `Pass` without `scenes`, as returned by GET /books/{slug}/passes. */
export type PassSummary = Schemas["PassSummary"];
export type VoiceOption = Schemas["VoiceOption"];
/** Settings.engines — the UI's only source of engine capability (v1.1). */
export type EngineOption = Schemas["EngineOption"];
export type Settings = Schemas["Settings"];
export type Health = Schemas["Health"];
/** GET /books/{slug}/cast. `shared_voices`: groups of characters sharing one voice. */
export type CastSummary = Schemas["CastSummary"];
/** GET/PUT /books/{slug}/lexicon. v1.1 `occurrences`: lines whose text contains the term. */
export type Lexicon = Schemas["LexiconView"];
/** GET /books/{slug}/versions. */
export type VersionEntry = Schemas["VersionEntry"];
/** PATCH /books/{slug}/lines/{id}. */
export type LinePatchResult = Schemas["LinePatchResult"];
/** POST /books/{slug}/lines/{id}/render. */
export type LineRenderResult = Schemas["LineRenderResult"];

// ---- responses: narrowed or widened on purpose ---------------------------------

/**
 * Chapter sign-off (`qc`) is optional here although v1.4 says it is always
 * sent: v1.2 asked clients to read a missing count as "this deployment does not
 * track sign-off", never as "nothing is approved", and the UI keeps honouring
 * that. `last_job` carries the typed `JobResult` below.
 */
export type BookSummary = Omit<Schemas["BookSummary"], "qc" | "last_job"> & {
  qc?: Schemas["QcCounts"];
  last_job: JobSummary | null;
};

/**
 * `qc_status` optional for the same reason as `BookSummary.qc`: read it through
 * `qcStatusOf()`, which defaults a studio that does not send it to "unheard".
 */
export type Chapter = Omit<Schemas["Chapter"], "qc_status"> & { qc_status?: QcStatus };

/** GET /books/{slug}/script — with the two widened types above. */
export type Script = Omit<Schemas["Script"], "book" | "chapters"> & {
  book: BookSummary;
  chapters: Chapter[];
};

/** One file in an ACX export result (v1.1). The job `result` is untyped on the wire. */
export interface AcxFile {
  file: string;
  url: string;
  bytes: number;
  chapter: number;
  duration_ms: number;
  measured: { integrated_lufs: number; true_peak_dbtp: number };
  checks: { peak_ok: boolean; loudness_ok: boolean; duration_ok: boolean };
}

/**
 * `result` by job kind (STUDIO-API.md, "result by kind"). The wire says only
 * "an object or null"; naming the shapes is documentation the screens rely on,
 * and the trailing `Record` keeps any other object assignable.
 */
export type JobResult =
  | { chapters_written: number[]; duration_ms: number }
  | { pass_id: string; counts: PassCounts }
  | { file: string; url: string; bytes: number; format: string; loudness: string }
  | { files: AcxFile[]; compliance: "pass" | "fail"; format: "acx" }
  | Record<string, unknown>;

export type JobSummary = Omit<Schemas["JobSummary"], "result"> & { result: JobResult | null };
export type Job = Omit<Schemas["Job"], "result"> & { result: JobResult | null };

/**
 * What `progress` / `done` SSE frames are typed as: a job record without its
 * `log` (the log arrives as its own events). `done` carries the whole Job on
 * the wire; typing both frames without `log` stops anyone reading a log off a
 * `progress` frame, which never has one.
 */
export type JobProgress = Omit<Job, "log">;

/**
 * GET /books/{slug}/exports. `measured` / `checks` are open maps on the wire
 * (the service also records LRA and room-tone levels); every ACX row carries at
 * least the keys named here, and every other row sends `null`.
 */
export type ExportFile = Omit<Schemas["ExportFile"], "measured" | "checks"> & {
  measured: ({ integrated_lufs: number; true_peak_dbtp: number } & Record<string, number>) | null;
  checks: { peak_ok: boolean; loudness_ok: boolean; duration_ok: boolean } | null;
};

/** GET /api/director/models (gateway — not part of the studio's export). */
export interface DirectorModels {
  provider: "ollama";
  models: { name: string; size_bytes: number; modified_at: string }[];
  default: string;
  error: string | null;
}

// ---- request bodies: what the UI sends (never null where the wire allows it) ---

export interface NewBookRequest {
  title: string;
  author: string;
  text: string;
  book_type: string; // "auto" | <type>
  voices: "auto" | "single" | "multi";
  casting_mode?: CastingMode;
}

export interface LinePatch {
  instruction?: string | null;
  spoken_text?: string | null;
  engine?: string;
  voice_id?: string | null;
  speed?: number;
  text?: string;
  // ---- Amendments v1.1 ---------------------------------------------------
  /** Reassign the line to an existing or new character. */
  speaker?: string;
  flag?: boolean;
  note?: string;
}

export interface ChapterPatch {
  title?: string;
  kind?: ChapterKind;
  include_in_audio?: boolean;
  /** v1.2. Stored in the qc.json sidecar: no new script version, no cache churn. */
  qc_status?: QcStatus;
}

export interface RenderRequest {
  scope: "all" | "stale" | "chapters" | "lines";
  chapters?: number[];
  line_ids?: string[];
}

export interface DirectRequest {
  scope: "all" | "undirected" | "chapters" | "scenes";
  chapters?: number[];
  scenes?: string[];
  provider?: "ollama" | "gemini";
  model?: string;
  target_engine?: string;
}

export interface ExportRequest {
  /** v1.1 adds "acx": one MP3 per included chapter, with a compliance table. */
  format?: "m4b" | "mp3" | "wav" | "acx";
  loudness?: "acx" | "none";
  sample_rate?: number;
  bitrate?: string;
}

export interface DecideRequest {
  decisions: {
    scene_id: string;
    decision: "accepted" | "edited" | "rejected" | "kept_text";
    instruction?: string;
  }[];
}

export interface SettingsPatch {
  narrator_engine?: string;
  expressive_engine?: string;
  casting_mode?: CastingMode;
  director?: Schemas["DirectorSettings-Input"];
  /** Replaces the stored export defaults wholesale; an absent `bitrate` is null. */
  export?: Schemas["ExportSettings-Input"];
}

// ---- the job stream (UI-side) ---------------------------------------------------

/**
 * Events delivered by GET /jobs/{id}/events (or synthesised by polling); see
 * jobStream.ts for the reconnect / polling / give-up rules.
 * `progress` / `done` frames are typed without `log` — see JobProgress.
 */
export interface JobStreamHandlers {
  /** A job snapshot. The first one after every (re)connect is complete: replace, don't merge. */
  onProgress: (job: JobProgress) => void;
  onLog: (entry: JobLogEntry) => void;
  /** The job reached done/failed/cancelled; the subscription has ended. */
  onDone: (job: JobProgress) => void;
  /** Something went wrong but the subscription keeps trying (a failed poll, an unreadable frame). */
  onError?: (message: string) => void;
  /** The stream dropped; reconnect `attempt` is scheduled in `delayMs`. */
  onReconnecting?: (attempt: number, delayMs: number) => void;
  /** A (re)connected stream delivered its first frame. */
  onLive?: () => void;
  /** Fired when the stream falls back to polling. */
  onFallback?: () => void;
  /** The subscription stopped for good; offer Retry, which is a new subscription. */
  onGiveUp?: (message: string) => void;
}
