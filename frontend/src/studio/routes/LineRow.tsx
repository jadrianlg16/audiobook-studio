import React from "react";

import type { Line } from "../api/types";
import { ATTRIBUTION_CONFIDENCE_FLOOR, isGuess } from "../domain";
import { StateLabel } from "../ui/bits";
import { DIRECTION_PLACEHOLDER } from "../ui/copy";
import { fmtDuration } from "../ui/format";

export interface LineRowProps {
  line: Line;
  selected: boolean;
  playing: boolean;
  /** This row's cached take is being auditioned. */
  auditioning: boolean;
  editing: boolean;
  /** Only ever the editing row's draft, so a keystroke re-renders one row. */
  draft: string;
  onSelect: (line: Line) => void;
  onBeginEdit: (line: Line) => void;
  onDraft: (value: string) => void;
  onCommit: (line: Line) => void;
  onCancelEdit: (line: Line) => void;
  onPlay: (line: Line) => void;
  onToggleFlag: (line: Line) => void;
}

/** "alternation · 0.4" — how the attributor decided, and how sure it was. */
function AttributionMark({ line }: { line: Line }) {
  const a = line.attribution;
  if (a.basis === "none") return null;
  const low = isGuess(a);
  return (
    <span
      className={`attr${low ? " low" : ""}`}
      title={
        low
          ? `Guessed from ${a.basis}, confidence ${a.confidence}. Below ${ATTRIBUTION_CONFIDENCE_FLOOR} the attributor abstains — check this speaker.`
          : `Speaker taken from ${a.basis}, confidence ${a.confidence}.`
      }
    >
      {a.basis} · {a.confidence.toFixed(2).replace(/0$/, "")}
    </span>
  );
}

/**
 * One line of the script.
 *
 * Not a `role="button"`: it contains buttons (the note, play, flag) and a
 * textarea, which is invalid inside a button role and made the row's accessible
 * name swallow all of them. Selection is by click or by j/k; `tabIndex={-1}`
 * exists so focus can be put back on the row after an edit.
 */
function LineRowInner({
  line,
  selected,
  playing,
  auditioning,
  editing,
  draft,
  onSelect,
  onBeginEdit,
  onDraft,
  onCommit,
  onCancelEdit,
  onPlay,
  onToggleFlag,
}: LineRowProps) {
  return (
    <div
      data-line={line.id}
      tabIndex={-1}
      className={`line ${selected ? "on" : ""} ${playing ? "playing" : ""} ${
        line.flag ? "flagged" : ""
      }`}
      onClick={() => onSelect(line)}
    >
      <div>
        <div className="headrow">
          <span className="chip speaker">{line.speaker}</span>
          <AttributionMark line={line} />
          {line.flag && (
            <span className="chip unknown" title="Flagged for a pickup">
              flagged
            </span>
          )}
        </div>

        <p className="text">{line.text}</p>

        {editing ? (
          <textarea
            className="noteedit"
            autoFocus
            rows={2}
            value={draft}
            onChange={(e) => onDraft(e.target.value)}
            onClick={(e) => e.stopPropagation()}
            onBlur={() => onCommit(line)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                onCommit(line);
              } else if (e.key === "Escape") {
                e.preventDefault();
                onCancelEdit(line);
              }
            }}
            placeholder={DIRECTION_PLACEHOLDER}
          />
        ) : line.render.instruction ? (
          <button
            type="button"
            className="dir"
            title="Edit this note"
            onClick={(e) => {
              e.stopPropagation();
              onBeginEdit(line);
            }}
          >
            ({line.render.instruction})
            {line.render.direction_source === "manual" && (
              <span className="mono faint" style={{ fontSize: 10, marginLeft: 8 }}>
                edited
              </span>
            )}
          </button>
        ) : (
          <button
            type="button"
            className="addnote"
            onClick={(e) => {
              e.stopPropagation();
              onBeginEdit(line);
            }}
          >
            + add direction
          </button>
        )}

        {line.note && <p className="qcnote">note: {line.note}</p>}
      </div>

      <div className="meta">
        <span className="row" style={{ gap: 4, flexWrap: "nowrap" }}>
          <button
            type="button"
            className="iconbtn"
            disabled={line.audio_state === "missing"}
            title={
              line.audio_state === "missing"
                ? "Never rendered — nothing cached to play"
                : "Play the cached take (no synth call)"
            }
            aria-label={`Play the cached take of line ${line.order + 1}`}
            onClick={(e) => {
              e.stopPropagation();
              onPlay(line);
            }}
          >
            {auditioning ? "❚❚" : "▶"}
          </button>
          <button
            type="button"
            className={`iconbtn ${line.flag ? "on" : ""}`}
            title="Flag this line for a pickup (f)"
            aria-pressed={line.flag}
            aria-label={`Flag line ${line.order + 1}`}
            onClick={(e) => {
              e.stopPropagation();
              onToggleFlag(line);
            }}
          >
            ⚑
          </button>
        </span>
        <span>{line.duration_ms ? fmtDuration(line.duration_ms) : "–"}</span>
        <StateLabel state={line.audio_state} />
      </div>
    </div>
  );
}

export const LineRow = React.memo(LineRowInner);
