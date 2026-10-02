import { Link } from "react-router-dom";

import type { CastEntry, Chapter, QcStatus } from "../api/types";
import { SHORTCUT_SCOPE } from "../ui/shortcuts";
import { qcStatusOf, SignOffButton } from "../ui/signoff";

export interface ChapterStat {
  directed: number;
  total: number;
  stale: number;
  missing: number;
  /** Lines matching the desk's current QC filter (undefined = no filter). */
  matches?: number;
}

/**
 * The desk's left rail: chapters (with their kind and whether they are in the
 * audio at all), the cast, and the way into the sheets that used to be
 * reachable only by hand-writing HTTP.
 */
export function ChapterRail({
  slug,
  chapters,
  currentIndex,
  stats,
  cast,
  licenceNote,
  filtering,
  busy,
  panel,
  onSelect,
  onToggleInclude,
  onSignOff,
  onPanel,
}: {
  slug: string;
  chapters: Chapter[];
  currentIndex: number;
  stats: Map<number, ChapterStat>;
  cast: CastEntry[];
  licenceNote: string | null;
  filtering: boolean;
  busy: boolean;
  panel: "exports" | "versions" | null;
  onSelect: (index: number) => void;
  onToggleInclude: (chapter: Chapter) => void;
  onSignOff: (chapter: Chapter, next: QcStatus) => void;
  onPanel: (panel: "exports" | "versions" | null) => void;
}) {
  return (
    <nav className="rail" aria-label="Chapters and cast" {...SHORTCUT_SCOPE}>
      <div className="sec">
        <span className="eyebrow">Chapters</span>
      </div>

      {chapters.map((c) => {
        const s = stats.get(c.index) ?? { directed: 0, total: 0, stale: 0, missing: 0 };
        const state = s.stale > 0 ? "stale" : s.missing > 0 ? "missing" : "cached";
        const skipped = !c.include_in_audio;
        return (
          <div className="railrow" key={c.index}>
            <button
              className={`railitem ${c.index === currentIndex ? "on" : ""} ${
                skipped ? "skipped" : ""
              }`}
              aria-current={c.index === currentIndex ? "true" : undefined}
              onClick={() => onSelect(c.index)}
            >
              <span className={`dot ${state}`} aria-hidden />
              <span className="t" title={`${c.index + 1} · ${c.title}`}>
                {c.index + 1} · {c.title}
                {c.kind !== "chapter" && (
                  <span className="faint"> · {c.kind.replace("_", " ")}</span>
                )}
              </span>
              <span className="n">
                {filtering && s.matches !== undefined ? (
                  <span style={{ color: "var(--signal)" }}>{s.matches} hit</span>
                ) : skipped ? (
                  "skipped"
                ) : (
                  `${s.directed}/${s.total}`
                )}
              </span>
            </button>
            {!skipped && (
              <SignOffButton
                compact
                status={qcStatusOf(c)}
                chapterNumber={c.index + 1}
                disabled={busy}
                onCycle={(next) => onSignOff(c, next)}
              />
            )}
            <button
              className="chaptertoggle"
              disabled={busy}
              title={
                skipped
                  ? "Include this chapter in the render and the export"
                  : "Skip this chapter: no render, no export, no chapter marker"
              }
              aria-label={`${skipped ? "Include" : "Skip"} chapter ${c.index + 1}`}
              onClick={() => onToggleInclude(c)}
            >
              {skipped ? "include" : "skip"}
            </button>
          </div>
        );
      })}

      <div
        className="sec"
        style={{ marginTop: 18, borderTop: "1px solid var(--rule)", paddingTop: 12 }}
      >
        <span className="eyebrow">Cast</span>
        <Link className="btn sm ghost" to={`/books/${slug}/cast`} style={{ marginLeft: "auto" }}>
          Cast sheet ›
        </Link>
      </div>
      {cast.map((c) => (
        <div key={c.character} className="railcast">
          <span style={{ color: "var(--ink)" }}>{c.character}</span>
          <span className="faint" style={{ marginLeft: "auto", textAlign: "right" }}>
            {c.voice_id ?? "by description"}
            <br />
            {c.engine}
          </span>
        </div>
      ))}

      <div
        className="sec"
        style={{ marginTop: 18, borderTop: "1px solid var(--rule)", paddingTop: 12 }}
      >
        <span className="eyebrow">Book</span>
      </div>
      <Link className="raillink" to={`/books/${slug}/lexicon`}>
        Pronunciation lexicon <span className="n">›</span>
      </Link>
      <button
        className="raillink"
        aria-expanded={panel === "exports"}
        onClick={() => onPanel(panel === "exports" ? null : "exports")}
      >
        Exports <span className="n">{panel === "exports" ? "×" : "›"}</span>
      </button>
      <button
        className="raillink"
        aria-expanded={panel === "versions"}
        onClick={() => onPanel(panel === "versions" ? null : "versions")}
      >
        Versions <span className="n">{panel === "versions" ? "×" : "›"}</span>
      </button>

      {licenceNote && (
        <div className="notice risk" style={{ margin: "16px 12px 0", fontSize: 11.5 }}>
          {licenceNote}
        </div>
      )}
    </nav>
  );
}
