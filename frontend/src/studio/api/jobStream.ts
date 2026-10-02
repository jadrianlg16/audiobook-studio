/**
 * Live job updates: GET /jobs/{id}/events, kept alive.
 *
 * A render runs for hours and a laptop sleeps, a proxy restarts, Wi-Fi drops.
 * The old client fell back to polling on the first stream error and stayed
 * there, with polls that could overlap and no way back after five failures.
 * Now:
 *
 * - A dropped stream reconnects with exponential backoff and jitter. That is
 *   safe because the service sends a full `progress` snapshot first on every
 *   connect (STUDIO-API.md), which simply replaces what the screen shows.
 * - Only after `streamFailuresBeforePolling` consecutive failures (a frame on a
 *   new connection resets the count) does it poll `GET /jobs/{id}` instead —
 *   serialized: the next poll is scheduled when the previous one has answered,
 *   so two can never be in flight.
 * - When polling fails `pollMaxFailures` times in a row, or the job is gone
 *   (404), it stops and says so through `onGiveUp`; the screen offers Retry,
 *   which is simply a new subscription.
 *
 * Frames are validated at runtime with zod. It is the one place the UI checks a
 * studio payload rather than trusting its type: a stream is long-lived and
 * outlives deploys, so a frame from a newer or older service must fail loudly
 * (`onError`) instead of rendering `undefined`. contract.ts ties these schemas
 * to the OpenAPI export at compile time.
 */
import { z } from "zod/mini";

import { isTerminalJob, JOB_STATUSES } from "../domain";
import { ApiError, errorMessage } from "./http";
import type { Job, JobLogEntry, JobStreamHandlers } from "./types";

// ---- runtime schemas ------------------------------------------------------------

const count = z.number();

const jobCounters = z.object({
  done: count,
  total: count,
  cache_hits: count,
  synth_calls: count,
  rtf: z.nullable(z.number()),
  elapsed_s: z.number(),
  eta_s: z.nullable(z.number()),
});

const jobChapter = z.object({
  index: z.number(),
  title: z.string(),
  done: count,
  total: count,
  status: z.enum(["queued", "running", "done", "failed", "skipped"]),
  seconds: z.number(),
  engines: z.record(z.string(), z.number()),
});

/** A `progress` or `done` frame. `done` also carries `log`; it is dropped here. */
export const jobFrameSchema = z.object({
  id: z.string(),
  kind: z.enum(["render", "direct", "export"]),
  book: z.string(),
  status: z.enum(JOB_STATUSES),
  created_at: z.string(),
  started_at: z.nullable(z.string()),
  finished_at: z.nullable(z.string()),
  progress: jobCounters,
  error: z.nullable(z.string()),
  result: z.nullable(z.record(z.string(), z.unknown())),
  chapters: z.array(jobChapter),
  request: z.record(z.string(), z.unknown()),
});

export const jobLogSchema = z.object({ ts: z.string(), line: z.string() });

/** GET /jobs/{id}, the polling fallback: a frame plus its log. */
const jobRecordSchema = z.extend(jobFrameSchema, { log: z.array(jobLogSchema) });

export type JobFrame = z.infer<typeof jobFrameSchema>;

// ---- timing -----------------------------------------------------------------------

export interface StreamTiming {
  /** First reconnect delay; doubles per consecutive failure. */
  reconnectBaseMs: number;
  /** Ceiling on the reconnect delay. */
  reconnectMaxMs: number;
  /** Consecutive stream failures before switching to polling. */
  streamFailuresBeforePolling: number;
  /** Gap between one poll's answer and the next poll. */
  pollMs: number;
  /** Consecutive failed polls before giving up. */
  pollMaxFailures: number;
}

export const STREAM_TIMING: StreamTiming = {
  reconnectBaseMs: 1_000,
  reconnectMaxMs: 15_000,
  streamFailuresBeforePolling: 4,
  pollMs: 2_000,
  pollMaxFailures: 5,
};

/**
 * Delay before reconnect attempt `attempt` (1-based): the exponential ceiling
 * `base · 2^(attempt-1)`, capped, with "equal jitter" — half of it fixed so the
 * delay still grows, half random so a fleet of tabs does not reconnect in step.
 */
export function backoffDelay(
  attempt: number,
  timing: Pick<StreamTiming, "reconnectBaseMs" | "reconnectMaxMs"> = STREAM_TIMING,
  random: () => number = Math.random,
): number {
  const ceiling = Math.min(timing.reconnectMaxMs, timing.reconnectBaseMs * 2 ** (attempt - 1));
  return Math.round(ceiling / 2 + random() * (ceiling / 2));
}

// ---- the subscription ---------------------------------------------------------------

export interface JobStreamOptions {
  /** GET /jobs/{id}/events */
  url: string;
  /** GET /jobs/{id}, for the polling fallback. */
  fetchJob: (signal: AbortSignal) => Promise<Job>;
  /** Injected in tests; the browser's by default. */
  EventSource?: typeof EventSource;
  random?: () => number;
  timing?: Partial<StreamTiming>;
}

const logKey = (e: JobLogEntry) => `${e.ts}|${e.line}`;

function describeIssues(issues: readonly { path: PropertyKey[]; message: string }[]): string {
  return issues
    .slice(0, 3)
    .map((i) => `${i.path.map(String).join(".") || "(frame)"}: ${i.message}`)
    .join("; ");
}

/** Subscribe to a job. Returns the unsubscribe function. */
export function openJobStream(options: JobStreamOptions, h: JobStreamHandlers): () => void {
  const timing = { ...STREAM_TIMING, ...options.timing };
  const random = options.random ?? Math.random;
  const Source = options.EventSource ?? globalThis.EventSource;

  let closed = false;
  let source: EventSource | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let polling = false;
  let inFlight: AbortController | null = null;
  /** Consecutive connections that failed before delivering a frame. */
  let streamFailures = 0;
  let pollFailures = 0;
  /** Last log entry handed to the caller, so the poll path can replay only the tail. */
  let lastLog: string | null = null;

  const stop = () => {
    closed = true;
    if (timer !== null) clearTimeout(timer);
    timer = null;
    source?.close();
    source = null;
    inFlight?.abort();
    inFlight = null;
  };

  const giveUp = (message: string) => {
    if (closed) return;
    stop();
    h.onGiveUp?.(message);
  };

  /** Validate one payload; a problem is described, never thrown. */
  function read<S extends z.ZodMiniType>(
    schema: S,
    data: unknown,
    what: string,
  ): { ok: true; value: z.infer<S> } | { ok: false; problem: string } {
    let raw: unknown = data;
    if (typeof data === "string") {
      try {
        raw = JSON.parse(data);
      } catch {
        return { ok: false, problem: `the studio sent a ${what} that is not JSON` };
      }
    }
    const parsed = schema.safeParse(raw);
    if (parsed.success) return { ok: true, value: parsed.data };
    return {
      ok: false,
      problem: `the studio sent a ${what} this screen cannot read — ${describeIssues(parsed.error.issues)}`,
    };
  }

  /**
   * The poll path only has the job record, so diff its `log` against what was
   * already emitted: this is exactly when the log matters most.
   */
  const replayLog = (entries: JobLogEntry[]) => {
    if (!entries.length) return;
    const at = lastLog === null ? -1 : entries.findIndex((e) => logKey(e) === lastLog);
    for (const e of at === -1 ? entries : entries.slice(at + 1)) h.onLog(e);
    lastLog = logKey(entries[entries.length - 1]);
  };

  // -- polling (serialized) --

  const poll = async () => {
    if (closed) return;
    const ctrl = new AbortController();
    inFlight = ctrl;
    try {
      const record = read(jobRecordSchema, await options.fetchJob(ctrl.signal), "job record");
      if (closed) return;
      // An unreadable record is a failed poll: counted, and given up on like one.
      if (!record.ok) throw new Error(record.problem);
      pollFailures = 0;
      const { log, ...frame } = record.value;
      h.onProgress(frame);
      replayLog(log);
      if (isTerminalJob(frame.status)) {
        stop();
        h.onDone(frame);
        return;
      }
    } catch (e) {
      if (closed) return;
      // A job that is gone will not come back by asking again.
      if (e instanceof ApiError && e.status === 404) return giveUp(errorMessage(e));
      pollFailures += 1;
      if (pollFailures >= timing.pollMaxFailures) {
        return giveUp(
          `no answer from the studio after ${pollFailures} attempts (${errorMessage(e)})`,
        );
      }
      h.onError?.(errorMessage(e));
    } finally {
      if (inFlight === ctrl) inFlight = null;
    }
    if (!closed) timer = setTimeout(() => void poll(), timing.pollMs);
  };

  const startPolling = () => {
    if (closed || polling) return;
    polling = true;
    source?.close();
    source = null;
    h.onFallback?.();
    void poll();
  };

  // -- the stream --

  const connect = () => {
    timer = null;
    if (closed) return;
    let es: EventSource;
    try {
      es = new Source(options.url);
    } catch {
      startPolling(); // no EventSource at all: nothing to retry
      return;
    }
    source = es;
    let live = false;
    const current = () => !closed && source === es;
    const markLive = () => {
      if (live) return;
      live = true;
      streamFailures = 0;
      h.onLive?.();
    };

    es.addEventListener("progress", (ev) => {
      if (!current()) return;
      const frame = read(jobFrameSchema, (ev as MessageEvent).data, "progress frame");
      if (!frame.ok) return h.onError?.(frame.problem);
      markLive();
      h.onProgress(frame.value);
    });
    es.addEventListener("log", (ev) => {
      if (!current()) return;
      const entry = read(jobLogSchema, (ev as MessageEvent).data, "log frame");
      if (!entry.ok) return h.onError?.(entry.problem);
      markLive();
      lastLog = logKey(entry.value);
      h.onLog(entry.value);
    });
    es.addEventListener("done", (ev) => {
      if (!current()) return;
      const frame = read(jobFrameSchema, (ev as MessageEvent).data, "done frame");
      // The service closes the stream after `done`; reconnecting would only
      // replay the same unreadable frame, so stop and say why.
      if (!frame.ok) return giveUp(frame.problem);
      stop();
      h.onDone(frame.value);
    });
    es.onerror = () => {
      if (!current()) return;
      // Take reconnection away from the browser: its retry has no backoff and
      // no limit. The first frame of the next connection is a full snapshot.
      es.close();
      source = null;
      streamFailures += 1;
      if (streamFailures >= timing.streamFailuresBeforePolling) {
        startPolling();
        return;
      }
      const delay = backoffDelay(streamFailures, timing, random);
      h.onReconnecting?.(streamFailures, delay);
      timer = setTimeout(connect, delay);
    };
  };

  connect();
  return stop;
}
