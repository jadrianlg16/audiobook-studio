/**
 * Runs before every test file (vitest.config.ts → setupFiles).
 *
 * jsdom lacks a few browser APIs the studio uses on mount; they are stubbed
 * here with the smallest thing that behaves, not with fake behaviour a test
 * could start to rely on.
 */
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, expect, vi } from "vitest";

import { consoleGuard } from "./console";

// useOverflowHint and VirtualRows observe sizes; jsdom has no layout, so an
// observer that never fires is the honest stand-in.
if (!("ResizeObserver" in globalThis)) {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
}

// scrollIntoViewSafely() calls this; jsdom does not implement scrolling.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {};
}

// The desk builds an <audio> for the chapter and pauses it on unmount. jsdom
// defines play() and pause() only to log "Not implemented", which the console
// guard below would count as a failure. Here pausing does nothing and playing
// refuses, as a browser does for media it cannot decode.
HTMLMediaElement.prototype.pause = function pause() {};
HTMLMediaElement.prototype.play = function play() {
  return Promise.reject(new DOMException("jsdom cannot play media", "NotSupportedError"));
};

// The mock API hands out its audio as blob: URLs; jsdom has no object URLs.
// Each call gets a distinct URL that points at nothing.
if (!URL.createObjectURL) {
  let made = 0;
  URL.createObjectURL = () => `blob:jsdom/${++made}`;
  URL.revokeObjectURL = () => {};
}

// A console.error during a test is almost always a real problem (an act()
// warning, a missing key, a crashed render), so it fails the test. A test that
// expects one says so with allowConsoleErrors() from ./console.
beforeEach(() => {
  consoleGuard.reset();
  vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
    consoleGuard.calls.push(args);
  });
});

afterEach(() => {
  // Vitest runs without globals, so Testing Library cannot register its own
  // cleanup: unmount here, before the console check, so an unmount-time error
  // is caught too.
  cleanup();
  const { calls, allowed } = consoleGuard;
  if (!allowed) {
    expect(
      calls.map((args) => args.map(String).join(" ")),
      "console.error was called during the test",
    ).toEqual([]);
  }
});
