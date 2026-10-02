import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useAsync } from "./bits";

/** A promise the test resolves or rejects by hand, plus the signal it was given. */
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** A loader whose every call is recorded and settled by the test. */
function controlledLoader<T>() {
  const calls: { signal: AbortSignal; d: ReturnType<typeof deferred<T>> }[] = [];
  const fn = vi.fn((signal: AbortSignal) => {
    const d = deferred<T>();
    calls.push({ signal, d });
    return d.promise;
  });
  return { fn, calls };
}

describe("useAsync", () => {
  it("loads, then exposes the data", async () => {
    const { fn, calls } = controlledLoader<string>();
    const { result } = renderHook(() => useAsync(fn, []));

    expect(result.current).toMatchObject({ data: null, error: null, loading: true });
    await act(async () => calls[0].d.resolve("the shelf"));
    expect(result.current).toMatchObject({ data: "the shelf", error: null, loading: false });
  });

  it("reports a failure as a message and stops loading", async () => {
    const { fn, calls } = controlledLoader<string>();
    const { result } = renderHook(() => useAsync(fn, []));

    await act(async () => calls[0].d.reject(new Error("HTTP 503 studio unavailable")));
    expect(result.current).toMatchObject({
      data: null,
      error: "HTTP 503 studio unavailable",
      loading: false,
    });
  });

  it("aborts the request in flight on unmount and sets nothing afterwards", async () => {
    const { fn, calls } = controlledLoader<string>();
    const { result, unmount } = renderHook(() => useAsync(fn, []));
    const before = result.current;

    unmount();
    expect(calls[0].signal.aborted).toBe(true);

    // A late answer (or the AbortError it would really be) must not land.
    await act(async () => calls[0].d.resolve("too late"));
    expect(result.current).toBe(before);
  });

  it("ignores an answer that arrives after a newer request was made", async () => {
    // The loader's promises never reject on abort, like a request that cannot
    // be cancelled: the stale answer really does arrive, and must be dropped.
    const { fn, calls } = controlledLoader<string>();
    const { result, rerender } = renderHook(
      ({ slug }) => useAsync((signal) => fn(signal).then((v) => `${slug}:${v}`), [slug]),
      { initialProps: { slug: "cold-lantern" } },
    );

    rerender({ slug: "harbour-road" });
    expect(calls).toHaveLength(2);

    // The newer request answers first, then the stale one straggles in.
    await act(async () => calls[1].d.resolve("script"));
    await act(async () => calls[0].d.resolve("script"));

    expect(result.current.data).toBe("harbour-road:script");
    expect(result.current.loading).toBe(false);
  });

  it("aborts the previous request when a dependency changes", async () => {
    const { fn, calls } = controlledLoader<string>();
    const { rerender } = renderHook(({ slug }) => useAsync((signal) => fn(signal), [slug]), {
      initialProps: { slug: "cold-lantern" },
    });

    rerender({ slug: "harbour-road" });
    expect(calls[0].signal.aborted).toBe(true);
    expect(calls[1].signal.aborted).toBe(false);
  });

  it("does not treat an aborted request as an error", async () => {
    const { fn, calls } = controlledLoader<string>();
    const { result, rerender } = renderHook(
      ({ slug }) => useAsync((signal) => fn(signal), [slug]),
      {
        initialProps: { slug: "a" },
      },
    );

    rerender({ slug: "b" });
    await act(async () => calls[0].d.reject(new DOMException("Aborted", "AbortError")));
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(true);

    await act(async () => calls[1].d.resolve("b"));
    expect(result.current).toMatchObject({ data: "b", error: null, loading: false });
  });

  it("reload() runs the loader again and replaces the data", async () => {
    let n = 0;
    const fn = vi.fn(async () => ++n);
    const { result } = renderHook(() => useAsync(fn, []));
    await waitFor(() => expect(result.current.data).toBe(1));

    act(() => result.current.reload());
    await waitFor(() => expect(result.current.data).toBe(2));
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("uses the latest loader without re-running when only its identity changes", async () => {
    const { result, rerender } = renderHook(({ label }) => useAsync(async () => label, []), {
      initialProps: { label: "first" },
    });
    await waitFor(() => expect(result.current.data).toBe("first"));

    // A new closure on every render is normal; it must not refetch by itself…
    rerender({ label: "second" });
    expect(result.current.data).toBe("first");

    // …but a reload uses the closure from the latest render.
    act(() => result.current.reload());
    await waitFor(() => expect(result.current.data).toBe("second"));
  });

  it("set() edits the data in place without a request", async () => {
    const fn = vi.fn(async () => ["a"]);
    const { result } = renderHook(() => useAsync(fn, []));
    await waitFor(() => expect(result.current.data).toEqual(["a"]));

    act(() => result.current.set((prev) => [...(prev ?? []), "b"]));
    expect(result.current.data).toEqual(["a", "b"]);
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
