/**
 * Speaker attribution: who says a line, and how sure the attributor was.
 * The pipeline abstains to the speaker "Unknown" below the confidence floor
 * (pipeline/attribute.py, ABSTAIN_BELOW); the desk's filter, the row's mark and
 * the inspector all flag the same lines by the same rule.
 */
import type { Attribution, Line } from "../api/types";

/** The speaker the attributor writes when it will not guess. */
export const UNKNOWN_SPEAKER = "Unknown";

/** Below this confidence the attribution is a guess worth checking. */
export const ATTRIBUTION_CONFIDENCE_FLOOR = 0.5;

/** The attributor was guessing. */
export function isGuess(attribution: Attribution): boolean {
  return attribution.confidence < ATTRIBUTION_CONFIDENCE_FLOOR;
}

/** A line whose speaker a person should check: abstained, or guessed. */
export function needsSpeakerCheck(line: Pick<Line, "speaker" | "attribution">): boolean {
  return line.speaker === UNKNOWN_SPEAKER || isGuess(line.attribution);
}
