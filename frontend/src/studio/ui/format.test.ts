import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fmtAgo, fmtBytes, fmtDuration, fmtSeconds, pct, shortKey, titleCase } from "./format";

const EN_DASH = "–";
const ELLIPSIS = "…";

describe("fmtDuration", () => {
  it("reads nothing, zero and nonsense as 0:00", () => {
    expect(fmtDuration(null)).toBe("0:00");
    expect(fmtDuration(undefined)).toBe("0:00");
    expect(fmtDuration(0)).toBe("0:00");
    expect(fmtDuration(-5000)).toBe("0:00");
  });

  it("pads seconds, and minutes once there are hours", () => {
    expect(fmtDuration(5_000)).toBe("0:05");
    expect(fmtDuration(61_000)).toBe("1:01");
    expect(fmtDuration(3_600_000)).toBe("1:00:00");
    expect(fmtDuration(3_661_000)).toBe("1:01:01");
  });

  it("rounds to the nearest second, carrying into the minute", () => {
    expect(fmtDuration(59_499)).toBe("0:59");
    expect(fmtDuration(59_500)).toBe("1:00");
  });
});

describe("fmtSeconds", () => {
  it("shows a dash for an unknown value but 0s for zero", () => {
    expect(fmtSeconds(null)).toBe(EN_DASH);
    expect(fmtSeconds(undefined)).toBe(EN_DASH);
    expect(fmtSeconds(0)).toBe("0s");
  });

  it("switches units at a minute and at an hour", () => {
    expect(fmtSeconds(5.4)).toBe("5s");
    expect(fmtSeconds(65)).toBe("1m 05s");
    expect(fmtSeconds(3599)).toBe("59m 59s");
    expect(fmtSeconds(3700)).toBe("1h 01m");
  });
});

describe("fmtAgo", () => {
  const NOW = new Date("2026-09-26T12:00:00Z");
  const before = (ms: number) => new Date(NOW.getTime() - ms).toISOString();

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("says never for a missing date and a dash for an unreadable one", () => {
    expect(fmtAgo(null)).toBe("never");
    expect(fmtAgo("not a date")).toBe(EN_DASH);
  });

  it("counts minutes, hours and days", () => {
    expect(fmtAgo(before(20_000))).toBe("just now");
    expect(fmtAgo(before(5 * 60_000))).toBe("5m ago");
    expect(fmtAgo(before(3 * 3_600_000))).toBe("3h ago");
    expect(fmtAgo(before(2 * 86_400_000))).toBe("2d ago");
  });

  it("falls back to a calendar date after a month", () => {
    const iso = before(40 * 86_400_000);
    expect(fmtAgo(iso)).toBe(new Date(iso).toLocaleDateString());
  });
});

describe("fmtBytes", () => {
  it("keeps bytes whole and gives larger units one decimal", () => {
    expect(fmtBytes(0)).toBe("0 B");
    expect(fmtBytes(1023)).toBe("1023 B");
    expect(fmtBytes(1536)).toBe("1.5 KB");
    expect(fmtBytes(48_213_004)).toBe("46.0 MB");
    expect(fmtBytes(5 * 1024 ** 3)).toBe("5.0 GB");
  });

  it("stops at GB rather than inventing a unit", () => {
    expect(fmtBytes(2048 * 1024 ** 3)).toBe("2048.0 GB");
  });
});

describe("shortKey", () => {
  it("shortens a cache key to its ends", () => {
    expect(shortKey("9f2c8b41a3d1")).toBe(`9f2c${ELLIPSIS}3d1`);
  });

  it("leaves a short key alone and dashes a missing one", () => {
    expect(shortKey("abc123def")).toBe("abc123def");
    expect(shortKey(null)).toBe(EN_DASH);
    expect(shortKey("")).toBe(EN_DASH);
  });
});

describe("pct", () => {
  it("rounds, guards a zero total and caps at 100", () => {
    expect(pct(1, 3)).toBe(33);
    expect(pct(2, 3)).toBe(67);
    expect(pct(5, 0)).toBe(0);
    expect(pct(7, 5)).toBe(100);
  });
});

describe("titleCase", () => {
  it("turns an enum value into a label", () => {
    expect(titleCase("general_fiction")).toBe("General fiction");
    expect(titleCase("dialogue_expressive")).toBe("Dialogue expressive");
    expect(titleCase("")).toBe("");
  });
});
