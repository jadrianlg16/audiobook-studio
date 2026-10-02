// "AI director" sample instructions. Each preset expresses one creative intent
// and is translated into each engine's *native* controls (expressiveness knob
// vs. emotion label), so a single direction drives every capable engine
// appropriately — and engines without emotion controls fall back to neutral.

import type { EngineUIState } from "./state";
import type { EngineStatus } from "./types";

export interface DirectorPreset {
  id: string;
  name: string;
  intent: string; // plain-language direction, shown to the user
  text: string;
  scalar: number; // 0..1 for engines with an expressiveness knob
  emotion: string; // matched (case-insensitive substring) vs. emotion_labels
}

export const PRESETS: DirectorPreset[] = [
  {
    id: "calm",
    name: "Calm narration",
    intent: "Neutral narrator — steady, unhurried, low intensity.",
    text: "The harbor was quiet before dawn, the water flat as poured glass.",
    scalar: 0.2,
    emotion: "calm",
  },
  {
    id: "tense",
    name: "Tense confrontation",
    intent: "Cold, clipped anger held just under the surface.",
    text: `"You're late," she said, not turning around. "Three years late."`,
    scalar: 0.85,
    emotion: "angry",
  },
  {
    id: "tender",
    name: "Tender reunion",
    intent: "Soft and vulnerable, almost a whisper — warmth under restraint.",
    text: `He stopped. The rope creaked. And then, softer than he meant it: "I came back."`,
    scalar: 0.4,
    emotion: "sad",
  },
  {
    id: "excited",
    name: "Excited announcement",
    intent: "Bright, fast, energetic — big and joyful.",
    text: `"We did it — we actually did it!" he shouted, laughing.`,
    scalar: 0.95,
    emotion: "happy",
  },
];

export interface AppliedDirection {
  engineId: string;
  parts: string[]; // what this engine actually received
}

/** A creative intent reduced to the two knobs engines understand. Both manual
 * presets and the LLM director produce this shape. */
export interface Direction {
  scalar: number; // 0..1 expressiveness
  emotion: string; // matched against emotion_labels
}

/** Translate a direction into one engine's native controls. */
export function applyDirectionToEngine(
  direction: Direction,
  engine: EngineStatus,
  prev: EngineUIState,
): { state: EngineUIState; applied: AppliedDirection } {
  const caps = engine.info?.capabilities;
  const next: EngineUIState = { ...prev };
  const parts: string[] = [];

  if (caps?.emotion_scalar) {
    next.emotion = direction.scalar;
    parts.push(`expressiveness ${direction.scalar.toFixed(2)}`);
  }
  if (caps?.emotion_tags && engine.info) {
    const match = engine.info.emotion_labels.find((l) =>
      l.toLowerCase().includes(direction.emotion.toLowerCase()),
    );
    if (match) {
      next.emotionLabel = match;
      parts.push(`emotion “${match}”`);
    }
  }
  if (parts.length === 0) parts.push("neutral (no emotion controls)");

  return { state: next, applied: { engineId: engine.id, parts } };
}
