import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./http";
import { backoffDelay, openJobStream, STREAM_TIMING } from "./jobStream";
import type { Job, JobStreamHandlers } from "./types";

// ---- a controllable EventSource --------------------------------------------------

class FakeEventSource {
  static made: FakeEventSource[] = [];
  readonly url: string;
  closed = false;
  onerror: ((ev: Event) => void) | null = null;
  private listeners = new Map<string, ((ev: MessageEvent) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.made.push(this);
  }
  addEventListener(type: string, fn: (ev: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn]);
  }
  close() {
    this.closed = true;
  }
  /** The server sends an event. */
  send(type: string, data: unknown) {
    const ev = new MessageEvent(type, {
      data: typeof data === "string" ? data : JSON.stringify(data),
    });
    for (const fn of this.listeners.get(type) ?? []) fn(ev);
  }
  /** The connection drops (or is refused). */
  drop() {
    this.onerror?.(new Event("error"));
  }
  static latest(): FakeEventSource {
    return FakeEventSource.made[FakeEventSource.made.length - 1];
  }
}

// ---- fixtures ---------------------------------------------------------------------

function job(done: number, status: Job["status"] = "running"): Job {
  return {
    id: "render-1",
    kind: "render",
    book: "cold-lantern",
    status,
    created_at: "2026-09-26T08:00:00Z",
    started_at: "2026-09-26T08:00:01Z",
    finished_at: status === "done" ? "2026-09-26T08:10:00Z" : null,
    progress: {
      done,
      total: 10,
      cache_hits: 0,
      synth_calls: done,
      rtf: null,
      elapsed_s: done,
      eta_s: null,
    },
    error: null,
    result: null,
    chapters: [
      { index: 0, title: "One", done, total: 10, status: "running", seconds: 1, engines: {} },
    ],
    request: { scope: "all" },
    log: [{ ts: "2026-09-26T08:00:02Z", line: `line ${done}` }],
  };
}
const frame = (done: number, status: Job["status"] = "running") => {
  const { log: _log, ...rest } = job(done, status);
  return rest;
};

function handlers() {
  return {
    onProgress: vi.fn(),
    onLog: vi.fn(),
    onDone: vi.fn(),
    onError: vi.fn(),
    onReconnecting: vi.fn(),
    onLive: vi.fn(),
    onFallback: vi.fn(),
    onGiveUp: vi.fn(),
  } satisfies JobStreamHandlers;
}

const HALF = () => 0.5; // jitter pinned to the middle: delays are deterministic

function open(h: JobStreamHandlers, fetchJob = vi.fn(async () => job(5)), random = HALF) {
  const stop = openJobStream(
    {
      url: "/studio/api/jobs/render-1/events",
      fetchJob,
      EventSource: FakeEventSource as unknown as typeof EventSource,
      random,
    },
    h,
  );
  return { stop, fetchJob };
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeEventSource.made = [];
});
afterEach(() => {
  vi.useRealTimers();
});

// ---- backoff ------------------------------------------------------------------------

describe("backoffDelay", () => {
  it("doubles per attempt up to the ceiling, with half of each delay random", () => {
    const timing = { reconnectBaseMs: 1000, reconnectMaxMs: 15000 };
    expect([1, 2, 3, 4, 5, 6].map((n) => backoffDelay(n, timing, HALF))).toEqual([
      750, 1500, 3000, 6000, 11250, 11250,
    ]);
    // jitter bounds: [ceiling/2, ceiling]
    expect(backoffDelay(3, timing, () => 0)).toBe(2000);
    expect(backoffDelay(3, timing, () => 1)).toBe(4000);
  });
});

// ---- the stream ---------------------------------------------------------------------

describe("openJobStream", () => {
  it("delivers progress, log and done, then closes", () => {
    const h = handlers();
    open(h);
    const es = FakeEventSource.latest();
    expect(es.url).toBe("/studio/api/jobs/render-1/events");

    es.send("progress", frame(1));
    es.send("log", { ts: "t1", line: "c0_s0_l0 synth kokoro" });
    // The wire's done frame is the whole Job; the log is dropped by the validator.
    es.send("done", job(10, "done"));

    expect(h.onLive).toHaveBeenCalledTimes(1);
    expect(h.onProgress).toHaveBeenCalledWith(frame(1));
    expect(h.onLog).toHaveBeenCalledWith({ ts: "t1", line: "c0_s0_l0 synth kokoro" });
    expect(h.onDone).toHaveBeenCalledWith(frame(10, "done"));
    expect(h.onDone.mock.calls[0][0]).not.toHaveProperty("log");
    expect(es.closed).toBe(true);
    // The server closes after `done`; that must not look like a drop.
    es.drop();
    vi.advanceTimersByTime(60_000);
    expect(FakeEventSource.made).toHaveLength(1);
  });

  it("reconnects after an error, and the first frame on the new stream replaces the state", () => {
    const h = handlers();
    open(h);
    const first = FakeEventSource.latest();
    first.send("progress", frame(2));
    first.drop();

    expect(first.closed).toBe(true); // the browser's own retry is switched off
    expect(h.onReconnecting).toHaveBeenCalledWith(1, 750);
    vi.advanceTimersByTime(749);
    expect(FakeEventSource.made).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeEventSource.made).toHaveLength(2);

    // The service opens every connection with a full snapshot.
    FakeEventSource.latest().send("progress", frame(7));
    expect(h.onProgress).toHaveBeenLastCalledWith(frame(7));
    expect(h.onLive).toHaveBeenCalledTimes(2);
    expect(h.onFallback).not.toHaveBeenCalled();
  });

  it("backs off longer after each consecutive failure, and a frame resets the count", () => {
    const h = handlers();
    open(h);
    FakeEventSource.latest().drop();
    vi.advanceTimersByTime(750);
    FakeEventSource.latest().drop();
    vi.advanceTimersByTime(1500);
    FakeEventSource.latest().drop();
    expect(h.onReconnecting.mock.calls).toEqual([
      [1, 750],
      [2, 1500],
      [3, 3000],
    ]);

    vi.advanceTimersByTime(3000);
    FakeEventSource.latest().send("progress", frame(3)); // connected: the slate is clean
    FakeEventSource.latest().drop();
    expect(h.onReconnecting).toHaveBeenLastCalledWith(1, 750);
  });

  it("falls back to polling only after N consecutive stream failures", async () => {
    const h = handlers();
    const { fetchJob } = open(h);
    for (let i = 1; i < STREAM_TIMING.streamFailuresBeforePolling; i++) {
      FakeEventSource.latest().drop();
      expect(h.onFallback).not.toHaveBeenCalled();
      vi.advanceTimersByTime(20_000);
    }
    expect(FakeEventSource.made).toHaveLength(STREAM_TIMING.streamFailuresBeforePolling);
    FakeEventSource.latest().drop();

    expect(h.onFallback).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchJob).toHaveBeenCalledTimes(1);
    expect(h.onProgress).toHaveBeenLastCalledWith(frame(5));
    expect(h.onLog).toHaveBeenCalledWith({ ts: "2026-09-26T08:00:02Z", line: "line 5" });
    // No stream is opened again while polling.
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeEventSource.made).toHaveLength(STREAM_TIMING.streamFailuresBeforePolling);
  });

  it("never has two polls in flight: the next is scheduled when the last answers", async () => {
    const h = handlers();
    let answer: (j: Job) => void = () => undefined;
    const fetchJob = vi.fn(
      () =>
        new Promise<Job>((resolve) => {
          answer = resolve;
        }),
    );
    open(h, fetchJob);
    for (let i = 0; i < STREAM_TIMING.streamFailuresBeforePolling; i++) {
      FakeEventSource.latest().drop();
      await vi.advanceTimersByTimeAsync(20_000);
    }
    expect(fetchJob).toHaveBeenCalledTimes(1);

    // A slow studio: ten poll intervals pass with the first request unanswered.
    await vi.advanceTimersByTimeAsync(STREAM_TIMING.pollMs * 10);
    expect(fetchJob).toHaveBeenCalledTimes(1);

    answer(job(6));
    await vi.advanceTimersByTimeAsync(0);
    expect(h.onProgress).toHaveBeenLastCalledWith(frame(6));
    expect(fetchJob).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(STREAM_TIMING.pollMs - 1);
    expect(fetchJob).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchJob).toHaveBeenCalledTimes(2);
  });

  it("gives up after the poll limit, and at once on a job that is gone", async () => {
    const h = handlers();
    const fetchJob = vi.fn(async (): Promise<Job> => {
      throw new ApiError(502, "Bad Gateway", "/studio/api/jobs/render-1");
    });
    open(h, fetchJob);
    for (let i = 0; i < STREAM_TIMING.streamFailuresBeforePolling; i++) {
      FakeEventSource.latest().drop();
      await vi.advanceTimersByTimeAsync(20_000);
    }
    await vi.advanceTimersByTimeAsync(STREAM_TIMING.pollMs * 10);
    expect(fetchJob).toHaveBeenCalledTimes(STREAM_TIMING.pollMaxFailures);
    expect(h.onError).toHaveBeenCalledTimes(STREAM_TIMING.pollMaxFailures - 1);
    expect(h.onGiveUp).toHaveBeenCalledTimes(1);
    expect(h.onGiveUp.mock.calls[0][0]).toMatch(/after 5 attempts \(Bad Gateway\)/);

    const gone = handlers();
    const missing = vi.fn(async (): Promise<Job> => {
      throw new ApiError(404, "no such job: 'render-1'", "/studio/api/jobs/render-1");
    });
    open(gone, missing);
    for (let i = 0; i < STREAM_TIMING.streamFailuresBeforePolling; i++) {
      FakeEventSource.latest().drop();
      await vi.advanceTimersByTimeAsync(20_000);
    }
    expect(missing).toHaveBeenCalledTimes(1);
    expect(gone.onGiveUp).toHaveBeenCalledWith("no such job: 'render-1'");
  });

  it("validates frames: an unreadable one is reported and never reaches the screen", () => {
    const h = handlers();
    open(h);
    const es = FakeEventSource.latest();
    es.send("progress", { ...frame(1), status: "exploded" });
    es.send("progress", "{not json");
    es.send("log", { ts: 12 });

    expect(h.onProgress).not.toHaveBeenCalled();
    expect(h.onLog).not.toHaveBeenCalled();
    expect(h.onError).toHaveBeenCalledTimes(3);
    expect(h.onError.mock.calls[0][0]).toMatch(/progress frame this screen cannot read — status/);
    expect(h.onError.mock.calls[1][0]).toMatch(/not JSON/);
    expect(h.onLive).not.toHaveBeenCalled(); // an unreadable frame does not count as connected
  });

  it("gives up on an unreadable done frame instead of reconnecting into it again", () => {
    const h = handlers();
    open(h);
    FakeEventSource.latest().send("done", { id: "render-1" });
    expect(h.onDone).not.toHaveBeenCalled();
    expect(h.onGiveUp).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(60_000);
    expect(FakeEventSource.made).toHaveLength(1);
  });

  it("stops everything on unsubscribe: the stream, a pending reconnect, a poll in flight", async () => {
    const h = handlers();
    const first = open(h);
    FakeEventSource.latest().drop(); // a reconnect is now pending
    first.stop();
    vi.advanceTimersByTime(60_000);
    expect(FakeEventSource.made).toHaveLength(1);

    let signal: AbortSignal | null = null;
    const fetchJob = vi.fn(
      (s: AbortSignal) =>
        new Promise<Job>(() => {
          signal = s;
        }),
    );
    const second = open(handlers(), fetchJob);
    for (let i = 0; i < STREAM_TIMING.streamFailuresBeforePolling; i++) {
      FakeEventSource.latest().drop();
      await vi.advanceTimersByTimeAsync(20_000);
    }
    expect(signal).not.toBeNull();
    second.stop();
    expect((signal as AbortSignal | null)?.aborted).toBe(true);
  });
});
