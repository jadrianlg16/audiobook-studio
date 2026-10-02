/**
 * The "render what is stale" action (STUDIO-API.md, Amendments v1.1): the shelf
 * row and the desk offer the same `POST /render {scope: "stale"}`, and name it
 * from the same two counts — lines whose audio is not cached, then chapters
 * whose WAV was not re-stitched since a line changed.
 */

/** `BookSummary.needs_render`, or the desk's own count of the same two things. */
export interface RenderNeeds {
  lines: number;
  chapters: number;
}

export interface RenderAction {
  /** What the stale render will do. "none" = nothing to render; disable the button. */
  kind: "lines" | "chapters" | "none";
  label: string;
  /** Tooltip: why the button says what it says. */
  title: string;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function renderAction(needs: RenderNeeds): RenderAction {
  if (needs.lines > 0) {
    return {
      kind: "lines",
      label: `Render ${plural(needs.lines, "stale line")}`,
      title:
        needs.lines === 1
          ? "1 line is stale or has never been rendered."
          : `${needs.lines} lines are stale or have never been rendered.`,
    };
  }
  if (needs.chapters > 0) {
    return {
      kind: "chapters",
      label: `Rebuild ${plural(needs.chapters, "chapter")}`,
      title:
        "Every line is cached, but these chapters' master audio has not been re-stitched since a line changed. Export needs them rebuilt.",
    };
  }
  return { kind: "none", label: "Nothing to render", title: "Every included chapter is fresh." };
}
