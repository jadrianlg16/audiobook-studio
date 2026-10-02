import type { BookSummary, Chapter, QcStatus } from "../api/types";

/**
 * Chapter sign-off (Amendment v1.2). The producer's question is not "is this
 * rendered" but "has a person heard it and did they accept it". That is a third
 * state the audio counters cannot express, so it is stored, not derived — and
 * any render that changes a chapter's audio resets it to "unheard".
 */
export const QC_ORDER: QcStatus[] = ["unheard", "approved", "pickups"];

export const QC_MARK: Record<QcStatus, string> = {
  unheard: "○",
  approved: "✓",
  pickups: "⚑",
};

export const QC_LABEL: Record<QcStatus, string> = {
  unheard: "unheard",
  approved: "approved",
  pickups: "pickups",
};

export const QC_MEANING: Record<QcStatus, string> = {
  unheard: "Nobody has listened to this chapter's audio yet.",
  approved: "Heard end to end and accepted.",
  pickups: "Heard, and something in it needs re-recording. Flag the lines (f) so the list is real.",
};

/** A studio older than v1.2 sends no status; that reads as "unheard", never as approved. */
export function qcStatusOf(chapter: Pick<Chapter, "qc_status"> | null | undefined): QcStatus {
  const s = chapter?.qc_status;
  return s === "approved" || s === "pickups" ? s : "unheard";
}

export function nextQcStatus(current: QcStatus): QcStatus {
  return QC_ORDER[(QC_ORDER.indexOf(current) + 1) % QC_ORDER.length];
}

/**
 * The three-state control. One button that cycles, because the rail has room
 * for one control per chapter and the states are ordered: nobody has heard it →
 * heard and fine → heard and not fine.
 */
export function SignOffButton({
  status,
  chapterNumber,
  disabled,
  compact,
  onCycle,
}: {
  status: QcStatus;
  /** 1-based, for the label a screen reader reads. */
  chapterNumber: number;
  disabled?: boolean;
  compact?: boolean;
  onCycle: (next: QcStatus) => void;
}) {
  const next = nextQcStatus(status);
  return (
    <button
      type="button"
      className={`qcbtn ${status}${compact ? " compact" : ""}`}
      disabled={disabled}
      aria-label={`Chapter ${chapterNumber} sign-off: ${QC_LABEL[status]}. Set to ${QC_LABEL[next]}.`}
      title={`Sign-off: ${QC_LABEL[status]} — ${QC_MEANING[status]}\nClick to set “${QC_LABEL[next]}”.`}
      onClick={(e) => {
        e.stopPropagation();
        e.preventDefault();
        onCycle(next);
      }}
    >
      <span aria-hidden>{QC_MARK[status]}</span>
      {!compact && <span className="qclabel">{QC_LABEL[status]}</span>}
    </button>
  );
}

/**
 * `qc.approved / qc.chapters` for a shelf row or a job screen. Absent when the
 * studio does not report it — a blank is honest, a "0/0" would not be.
 */
export function SignOffSummary({ qc }: { qc: BookSummary["qc"] }) {
  if (!qc) return <span className="faint">–</span>;
  return (
    <span
      className="mono"
      title={`${qc.approved} of ${qc.chapters} included chapters approved${
        qc.pickups > 0 ? `, ${qc.pickups} marked for pickups` : ""
      }.`}
    >
      <span
        style={{
          color: qc.approved === qc.chapters && qc.chapters > 0 ? "var(--signal)" : undefined,
        }}
      >
        {qc.approved}
      </span>
      <span className="faint">/{qc.chapters}</span>
      {qc.pickups > 0 && (
        <span style={{ color: "var(--risk)" }} title={`${qc.pickups} chapters need pickups`}>
          {" "}
          ⚑{qc.pickups}
        </span>
      )}
    </span>
  );
}
