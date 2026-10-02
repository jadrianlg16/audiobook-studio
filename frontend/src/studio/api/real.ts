import type { StudioApi } from "./client";
import { errorMessage, http, jsonBody } from "./http";
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

// Same-origin in production (nginx proxies both); Vite proxies them in dev.
const STUDIO = "/studio/api";
const BOOKS = "/studio/books";
const GATEWAY = "/api";

// Every call goes through http(): a non-2xx answer is an ApiError carrying the
// status, the studio's `detail` and the URL.
const req = http;
const json = (body: unknown) => jsonBody("POST", body);
const patch = (body: unknown) => jsonBody("PATCH", body);
const put = (body: unknown) => jsonBody("PUT", body);

export function createRealApi(): StudioApi {
  return {
    isMock: false,

    listBooks: (signal?: AbortSignal) => req<BookSummary[]>(`${STUDIO}/books`, { signal }),
    createBook: (body: NewBookRequest) => req<BookSummary>(`${STUDIO}/books`, json(body)),
    uploadBook: (form: FormData) =>
      req<BookSummary>(`${STUDIO}/books/upload`, { method: "POST", body: form }),
    getBook: (slug: Slug, signal?: AbortSignal) =>
      req<BookSummary>(`${STUDIO}/books/${slug}`, { signal }),
    deleteBook: (slug: Slug) => req<void>(`${STUDIO}/books/${slug}`, { method: "DELETE" }),
    getScript: (slug: Slug, signal?: AbortSignal) =>
      req<Script>(`${STUDIO}/books/${slug}/script`, { signal }),

    patchLine: (slug: Slug, lineId: string, body: LinePatch) =>
      req<{ line: Line; version: number }>(`${STUDIO}/books/${slug}/lines/${lineId}`, patch(body)),
    patchChapter: (slug: Slug, index: number, body: ChapterPatch) =>
      req<Chapter>(`${STUDIO}/books/${slug}/chapters/${index}`, patch(body)),
    renderLine: (slug: Slug, lineId: string) =>
      req<{ line: Line; audio_url: string }>(
        `${STUDIO}/books/${slug}/lines/${lineId}/render`,
        json({}),
      ),
    lineAudioUrl: (slug: Slug, lineId: string) => `${STUDIO}/books/${slug}/lines/${lineId}/audio`,
    bookFileUrl: (slug: Slug, file: string) => `${BOOKS}/${slug}/${file}`,

    getCast: (slug: Slug, signal?: AbortSignal) =>
      req<CastSummary>(`${STUDIO}/books/${slug}/cast`, { signal }),
    patchCast: (slug: Slug, entries: CastEntry[]) =>
      req<Script>(`${STUDIO}/books/${slug}/cast`, patch(entries)),
    mergeCast: (slug: Slug, from: string, into: string) =>
      req<Script>(`${STUDIO}/books/${slug}/cast/merge`, json({ from, into })),
    getLexicon: (slug: Slug, signal?: AbortSignal) =>
      req<Lexicon>(`${STUDIO}/books/${slug}/lexicon`, { signal }),
    putLexicon: (slug: Slug, entries: Record<string, string>) =>
      req<Lexicon>(`${STUDIO}/books/${slug}/lexicon`, put({ entries })),

    listVersions: (slug: Slug, signal?: AbortSignal) =>
      req<VersionEntry[]>(`${STUDIO}/books/${slug}/versions`, { signal }),
    restoreVersion: (slug: Slug, version: number) =>
      req<Script>(`${STUDIO}/books/${slug}/versions/${version}/restore`, json({})),

    render: (slug: Slug, body: RenderRequest) =>
      req<Job>(`${STUDIO}/books/${slug}/render`, json(body)),
    direct: (slug: Slug, body: DirectRequest) =>
      req<Job>(`${STUDIO}/books/${slug}/direct`, json(body)),
    exportBook: (slug: Slug, body: ExportRequest) =>
      req<Job>(`${STUDIO}/books/${slug}/export`, json(body)),
    listExports: (slug: Slug, signal?: AbortSignal) =>
      req<ExportFile[]>(`${STUDIO}/books/${slug}/exports`, { signal }),

    listPasses: (slug: Slug, signal?: AbortSignal) =>
      req<PassSummary[]>(`${STUDIO}/books/${slug}/passes`, { signal }),
    getPass: (slug: Slug, passId: string, signal?: AbortSignal) =>
      req<Pass>(`${STUDIO}/books/${slug}/passes/${passId}`, { signal }),
    decide: (slug: Slug, passId: string, body: DecideRequest) =>
      req<Pass>(`${STUDIO}/books/${slug}/passes/${passId}/decide`, json(body)),
    applyPass: (slug: Slug, passId: string) =>
      req<{ pass: Pass; script: Script }>(
        `${STUDIO}/books/${slug}/passes/${passId}/apply`,
        json({}),
      ),
    retryPass: (slug: Slug, passId: string, scenes: string[]) =>
      req<Job>(`${STUDIO}/books/${slug}/passes/${passId}/retry`, json({ scenes })),

    listJobs: (signal?: AbortSignal) => req<JobSummary[]>(`${STUDIO}/jobs`, { signal }),
    getJob: (id: string, signal?: AbortSignal) => req<Job>(`${STUDIO}/jobs/${id}`, { signal }),
    pauseJob: (id: string) => req<Job>(`${STUDIO}/jobs/${id}/pause`, json({})),
    resumeJob: (id: string) => req<Job>(`${STUDIO}/jobs/${id}/resume`, json({})),
    cancelJob: (id: string) => req<Job>(`${STUDIO}/jobs/${id}/cancel`, json({})),

    /**
     * jobStream.ts (and zod, its runtime validator) is fetched on the first
     * subscription, so only a job screen pays for it. A failed chunk load
     * reads as a stream that gave up: the screen's Retry tries again.
     */
    subscribeJob(id: string, h: JobStreamHandlers) {
      let stop: (() => void) | null = null;
      let cancelled = false;
      import("./jobStream")
        .then(({ openJobStream }) => {
          if (cancelled) return;
          stop = openJobStream(
            {
              url: `${STUDIO}/jobs/${id}/events`,
              fetchJob: (signal) => req<Job>(`${STUDIO}/jobs/${id}`, { signal }),
            },
            h,
          );
        })
        .catch((e: unknown) => {
          if (!cancelled) h.onGiveUp?.(errorMessage(e));
        });
      return () => {
        cancelled = true;
        stop?.();
      };
    },

    getSettings: (signal?: AbortSignal) => req<Settings>(`${STUDIO}/settings`, { signal }),
    patchSettings: (body: SettingsPatch) => req<Settings>(`${STUDIO}/settings`, patch(body)),
    health: (signal?: AbortSignal) => req<Health>(`${STUDIO}/health`, { signal }),

    /**
     * v1.1: the studio's flattened copy is the source of truth. The gateway's
     * /api/engines returns `info: null` for any engine whose container is down,
     * which silently cost the UI the non-commercial badge, the audio-tag panel
     * and the "takes direction" capability of the one engine that has them.
     */
    async listEngines(signal?: AbortSignal): Promise<EngineOption[]> {
      return (await req<Settings>(`${STUDIO}/settings`, { signal })).engines ?? [];
    },
    directorModels: (signal?: AbortSignal) =>
      req<DirectorModels>(`${GATEWAY}/director/models`, { signal }),
  };
}
