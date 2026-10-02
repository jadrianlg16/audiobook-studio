import { describe, expect, it } from "vitest";

import { sideParts, wordDiff } from "./diff";
import type { DiffPart } from "./diff";

const join = (parts: DiffPart[]) => parts.map((p) => p.text).join("");

describe("wordDiff", () => {
  it("marks a one-word substitution and keeps everything around it", () => {
    const parts = wordDiff(
      "She put the letter down and did not pick it up again.",
      "She set the letter down and did not pick it up again.",
    );
    expect(parts).toEqual([
      { op: "same", text: "She " },
      { op: "del", text: "put" },
      { op: "ins", text: "set" },
      { op: "same", text: " the letter down and did not pick it up again." },
    ]);
  });

  it("shows an appended tag as an insertion", () => {
    const parts = wordDiff("He waited.", "He waited. (sigh)");
    expect(parts.filter((p) => p.op === "ins").map((p) => p.text.trim())).toEqual(["(sigh)"]);
    expect(parts.some((p) => p.op === "del")).toBe(false);
  });

  it("treats case and punctuation as noise and shows the model's spelling", () => {
    const parts = wordDiff(
      "Then say it plainly, and I will believe you.",
      "then say it plainly and I will believe you",
    );
    expect(parts).toEqual([{ op: "same", text: "then say it plainly and I will believe you" }]);
  });

  it("ignores curly and straight quotes alike", () => {
    expect(wordDiff("“Then”", '"Then"')).toEqual([{ op: "same", text: '"Then"' }]);
    expect(wordDiff("‘tis", "'tis")).toEqual([{ op: "same", text: "'tis" }]);
  });

  it("handles empty input on either side", () => {
    expect(wordDiff("", "")).toEqual([]);
    expect(wordDiff("", "new words")).toEqual([{ op: "ins", text: "new words" }]);
    expect(wordDiff("old words", "")).toEqual([{ op: "del", text: "old words" }]);
  });

  it("merges neighbouring parts of the same kind", () => {
    const parts = wordDiff("a b c", "x y z");
    // Never two neighbours with the same op: the renderer relies on it.
    for (let i = 1; i < parts.length; i++) expect(parts[i].op).not.toBe(parts[i - 1].op);
  });

  it("always reassembles the model's text exactly from the 'after' side", () => {
    const cases: [string, string][] = [
      ["She put the letter down.", "She set the letter down. (sigh)"],
      ["one  two\tthree", "one two three four"],
      ["Keep calm, she said.", "(sigh) keep calm she said"],
      ["", "only after"],
    ];
    for (const [before, after] of cases) {
      expect(join(sideParts(wordDiff(before, after), "after"))).toBe(after);
    }
  });

  it("reassembles the book's text from the 'before' side when only words changed", () => {
    const before = "She folded the map along its old creases and put it away.";
    const after = "She folded the map along its creases and put it away. (sigh)";
    expect(join(sideParts(wordDiff(before, after), "before"))).toBe(before);
  });
});

describe("sideParts", () => {
  const parts: DiffPart[] = [
    { op: "same", text: "a " },
    { op: "del", text: "b" },
    { op: "ins", text: "c" },
  ];

  it("drops insertions from the book side and deletions from the model side", () => {
    expect(sideParts(parts, "before")).toEqual([parts[0], parts[1]]);
    expect(sideParts(parts, "after")).toEqual([parts[0], parts[2]]);
  });
});
