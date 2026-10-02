import { describe, expect, it } from "vitest";

import type { JobStatus } from "../api/types";
import {
  ATTRIBUTION_CONFIDENCE_FLOOR,
  CASTING_MODES,
  castingModeLabel,
  isCastingMode,
  isGuess,
  isLiveJob,
  isResumableJob,
  isTerminalJob,
  JOB_STATUSES,
  needsSpeakerCheck,
  renderAction,
  UNKNOWN_SPEAKER,
} from "./index";

describe("job statuses", () => {
  const where = (test: (s: JobStatus) => boolean) => JOB_STATUSES.filter(test);

  it("names the terminal, live and resumable sets once", () => {
    expect(where(isTerminalJob)).toEqual(["done", "failed", "cancelled"]);
    expect(where(isLiveJob)).toEqual(["queued", "running", "paused"]);
    expect(where(isResumableJob)).toEqual(["paused", "failed", "interrupted"]);
  });

  it("keeps interrupted out of both terminal and live: it waits for a resume", () => {
    expect(isTerminalJob("interrupted")).toBe(false);
    expect(isLiveJob("interrupted")).toBe(false);
    expect(isResumableJob("interrupted")).toBe(true);
  });

  it("puts every status in exactly one of terminal / live / interrupted", () => {
    for (const s of JOB_STATUSES) {
      const buckets = [isTerminalJob(s), isLiveJob(s), s === "interrupted"].filter(Boolean);
      expect(buckets, s).toHaveLength(1);
    }
  });
});

describe("speaker attribution", () => {
  it("abstains below the floor, not at it", () => {
    expect(ATTRIBUTION_CONFIDENCE_FLOOR).toBe(0.5);
    expect(isGuess({ basis: "alternation", confidence: 0.49 })).toBe(true);
    expect(isGuess({ basis: "tag", confidence: 0.5 })).toBe(false);
  });

  it("flags Unknown and guessed speakers, and nothing else", () => {
    const sure = { basis: "tag", confidence: 0.92 } as const;
    expect(needsSpeakerCheck({ speaker: UNKNOWN_SPEAKER, attribution: sure })).toBe(true);
    expect(
      needsSpeakerCheck({ speaker: "Marcus", attribution: { basis: "pronoun", confidence: 0.4 } }),
    ).toBe(true);
    expect(needsSpeakerCheck({ speaker: "Marcus", attribution: sure })).toBe(false);
    expect(
      needsSpeakerCheck({ speaker: "Narrator", attribution: { basis: "none", confidence: 1 } }),
    ).toBe(false);
  });
});

describe("casting modes", () => {
  it("lists the three modes in the order the forms offer them", () => {
    expect(CASTING_MODES).toEqual(["narrator_only", "dialogue_expressive", "all_expressive"]);
  });

  it("labels a mode by name for the New book form and by id for Settings", () => {
    expect(castingModeLabel("dialogue_expressive")).toBe(
      "Dialogue expressive — narration clean, dialogue directed",
    );
    expect(castingModeLabel("narrator_only", "id")).toBe(
      "narrator_only — one engine for the whole book",
    );
  });

  it("recognises a stored mode and refuses anything else", () => {
    expect(isCastingMode("all_expressive")).toBe(true);
    expect(isCastingMode("everything")).toBe(false);
    expect(isCastingMode("")).toBe(false);
  });
});

describe("the stale-render action", () => {
  it("renders lines first, with the count", () => {
    expect(renderAction({ lines: 3, chapters: 2 })).toEqual({
      kind: "lines",
      label: "Render 3 stale lines",
      title: "3 lines are stale or have never been rendered.",
    });
    expect(renderAction({ lines: 1, chapters: 0 })).toMatchObject({
      label: "Render 1 stale line",
      title: "1 line is stale or has never been rendered.",
    });
  });

  it("rebuilds chapters when every line is cached but a chapter WAV is not fresh", () => {
    const action = renderAction({ lines: 0, chapters: 1 });
    expect(action.kind).toBe("chapters");
    expect(action.label).toBe("Rebuild 1 chapter");
    expect(action.title).toMatch(/re-stitched/);
    expect(renderAction({ lines: 0, chapters: 4 }).label).toBe("Rebuild 4 chapters");
  });

  it("has nothing to do when the book is fresh", () => {
    expect(renderAction({ lines: 0, chapters: 0 })).toEqual({
      kind: "none",
      label: "Nothing to render",
      title: "Every included chapter is fresh.",
    });
  });
});
