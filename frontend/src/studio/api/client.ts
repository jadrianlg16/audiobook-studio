import { createContext, useContext } from "react";

import type {
  BookSummary,
  CastEntry,
  CastSummary,
  Chapter,
  ChapterPatch,
  DecideRequest,
  DirectRequest,
  DirectorModels,
  EngineOption,
  ExportFile,
  ExportRequest,
  Health,
  Job,
  JobStreamHandlers,
  JobSummary,
  Lexicon,
  Line,
  LinePatch,
  NewBookRequest,
  Pass,
  PassSummary,
  RenderRequest,
  Script,
  Settings,
  SettingsPatch,
  Slug,
  VersionEntry,
} from "./types";

/**
 * Every call the UI is allowed to make. One method per row of the endpoint
 * tables in docs/STUDIO-API.md (including Amendments v1.1 and v1.2); nothing
 * else exists.
 *
 * Every read takes an optional `AbortSignal`. `useAsync` passes one and aborts
 * it on unmount or on a dependency change, so a screen you navigated away from
 * actually cancels its request instead of merely ignoring the answer — which
 * matters most for `getScript` on a long book.
 */
export interface StudioApi {
  readonly isMock: boolean;

  // books
  listBooks(signal?: AbortSignal): Promise<BookSummary[]>;
  createBook(body: NewBookRequest): Promise<BookSummary>;
  uploadBook(form: FormData): Promise<BookSummary>;
  getBook(slug: Slug, signal?: AbortSignal): Promise<BookSummary>;
  deleteBook(slug: Slug): Promise<void>;
  getScript(slug: Slug, signal?: AbortSignal): Promise<Script>;
  patchLine(slug: Slug, lineId: string, patch: LinePatch): Promise<{ line: Line; version: number }>;
  patchChapter(slug: Slug, index: number, patch: ChapterPatch): Promise<Chapter>;
  renderLine(slug: Slug, lineId: string): Promise<{ line: Line; audio_url: string }>;
  /** URL for GET /books/{slug}/lines/{id}/audio — the cached take, no synth call. */
  lineAudioUrl(slug: Slug, lineId: string): string;
  /** URL for a rendered chapter WAV under /studio/books/<slug>/<file>. */
  bookFileUrl(slug: Slug, file: string): string;

  // cast & lexicon
  getCast(slug: Slug, signal?: AbortSignal): Promise<CastSummary>;
  patchCast(slug: Slug, entries: CastEntry[]): Promise<Script>;
  mergeCast(slug: Slug, from: string, into: string): Promise<Script>;
  getLexicon(slug: Slug, signal?: AbortSignal): Promise<Lexicon>;
  putLexicon(slug: Slug, entries: Record<string, string>): Promise<Lexicon>;

  // versions
  listVersions(slug: Slug, signal?: AbortSignal): Promise<VersionEntry[]>;
  restoreVersion(slug: Slug, version: number): Promise<Script>;

  // rendering
  render(slug: Slug, body: RenderRequest): Promise<Job>;
  direct(slug: Slug, body: DirectRequest): Promise<Job>;
  exportBook(slug: Slug, body: ExportRequest): Promise<Job>;
  listExports(slug: Slug, signal?: AbortSignal): Promise<ExportFile[]>;

  // passes
  listPasses(slug: Slug, signal?: AbortSignal): Promise<PassSummary[]>;
  getPass(slug: Slug, passId: string, signal?: AbortSignal): Promise<Pass>;
  decide(slug: Slug, passId: string, body: DecideRequest): Promise<Pass>;
  applyPass(slug: Slug, passId: string): Promise<{ pass: Pass; script: Script }>;
  retryPass(slug: Slug, passId: string, scenes: string[]): Promise<Job>;

  // jobs
  listJobs(signal?: AbortSignal): Promise<JobSummary[]>;
  getJob(id: string, signal?: AbortSignal): Promise<Job>;
  pauseJob(id: string): Promise<Job>;
  resumeJob(id: string): Promise<Job>;
  cancelJob(id: string): Promise<Job>;
  /** Live job updates. Returns an unsubscribe function. */
  subscribeJob(id: string, handlers: JobStreamHandlers): () => void;

  // settings & meta
  getSettings(signal?: AbortSignal): Promise<Settings>;
  patchSettings(patch: SettingsPatch): Promise<Settings>;
  health(signal?: AbortSignal): Promise<Health>;

  /**
   * Engine capabilities. v1.1: this is `Settings.engines` from the studio, never
   * the gateway's /api/engines (which returns `info: null`, and so no licence, no
   * tags and no `instruction`, for every engine that is not currently running).
   */
  listEngines(signal?: AbortSignal): Promise<EngineOption[]>;
  // gateway
  directorModels(signal?: AbortSignal): Promise<DirectorModels>;
}

export const ApiContext = createContext<StudioApi | null>(null);

export function useApi(): StudioApi {
  const api = useContext(ApiContext);
  if (!api) throw new Error("useApi() outside <ApiContext.Provider>");
  return api;
}

// ---- mock selection -------------------------------------------------------

const MOCK_KEY = "studioMock";

/** Development builds (or an explicit VITE_MOCK=1 build) may remember ?mock=1. */
const MOCK_IS_STICKY = import.meta.env.DEV || import.meta.env.VITE_MOCK === "1";

/**
 * Mock data is on when the bundle was built with VITE_MOCK=1, or when the URL
 * carries ?mock=1.
 *
 * Only a dev/mock build remembers the choice in localStorage: in a production
 * bundle one stray query param used to latch that browser into invented books
 * until someone thought to visit ?mock=0. In production the flag now lives for
 * exactly one page load.
 */
export function resolveMockMode(): boolean {
  if (import.meta.env.VITE_MOCK === "1") return true;
  try {
    const params = new URLSearchParams(window.location.search);
    const q = params.get("mock");
    if (q === "1" || q === "0") {
      if (MOCK_IS_STICKY) window.localStorage.setItem(MOCK_KEY, q);
      return q === "1";
    }
    if (!MOCK_IS_STICKY) return false;
    return window.localStorage.getItem(MOCK_KEY) === "1";
  } catch {
    // private mode / storage disabled: fall through to the real API
    return false;
  }
}
