/**
 * Word-level diff, small enough not to be worth a dependency.
 *
 * Scene passages are a few dozen words, so a plain LCS table is fine: the
 * point is only to make "put" -> "set" legible at a glance on the review
 * screen.
 */

export type DiffOp = "same" | "ins" | "del";
export interface DiffPart {
  op: DiffOp;
  text: string;
}

/** Split into words and the whitespace between them, keeping both. */
function tokenize(s: string): string[] {
  return s.split(/(\s+)/).filter((t) => t.length > 0);
}

/** Punctuation and case are noise when the question is "did it rewrite this". */
function norm(t: string): string {
  return t.toLowerCase().replace(/[.,;:!?"'“”‘’()]/g, "");
}

export function wordDiff(before: string, after: string): DiffPart[] {
  const a = tokenize(before);
  const b = tokenize(after);
  const n = a.length;
  const m = b.length;

  // LCS lengths
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] =
        norm(a[i]) === norm(b[j]) ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }

  const parts: DiffPart[] = [];
  const push = (op: DiffOp, text: string) => {
    const last = parts[parts.length - 1];
    if (last && last.op === op) last.text += text;
    else parts.push({ op, text });
  };

  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (norm(a[i]) === norm(b[j])) {
      // identical after normalising: show the "after" spelling
      push("same", b[j]);
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      push("del", a[i]);
      i++;
    } else {
      push("ins", b[j]);
      j++;
    }
  }
  while (i < n) push("del", a[i++]);
  while (j < m) push("ins", b[j++]);
  return parts;
}

/** Only the parts belonging to one side, for rendering "book" vs "model". */
export function sideParts(parts: DiffPart[], side: "before" | "after"): DiffPart[] {
  const drop: DiffOp = side === "before" ? "ins" : "del";
  return parts.filter((p) => p.op !== drop);
}
