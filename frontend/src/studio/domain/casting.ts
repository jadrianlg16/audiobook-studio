/**
 * The three casting modes (pipeline/cast.py `assign_engines`), named once. The
 * New book form shows the readable name, Settings shows the stored id; both
 * describe a mode the same way.
 */
import type { CastingMode } from "../api/types";

export const CASTING_MODES = [
  "narrator_only",
  "dialogue_expressive",
  "all_expressive",
] as const satisfies readonly CastingMode[];

type Missing = Exclude<CastingMode, (typeof CASTING_MODES)[number]>;
const _exhaustive: [Missing] extends [never] ? true : Missing = true;
void _exhaustive;

export const CASTING_MODE_INFO: Record<CastingMode, { name: string; summary: string }> = {
  narrator_only: { name: "Narrator only", summary: "one engine for the whole book" },
  dialogue_expressive: {
    name: "Dialogue expressive",
    summary: "narration clean, dialogue directed",
  },
  all_expressive: { name: "All expressive", summary: "every line on the expressive engine" },
};

/**
 * "Narrator only — one engine for the whole book" (`by: "name"`, the New book
 * form) or "narrator_only — one engine for the whole book" (`by: "id"`,
 * Settings, where the id is what gets stored).
 */
export function castingModeLabel(mode: CastingMode, by: "name" | "id" = "name"): string {
  const info = CASTING_MODE_INFO[mode];
  return `${by === "id" ? mode : info.name} — ${info.summary}`;
}

export function isCastingMode(value: string): value is CastingMode {
  return (CASTING_MODES as readonly string[]).includes(value);
}
