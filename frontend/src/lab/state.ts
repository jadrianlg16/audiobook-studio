// Per-engine UI state for the bake-off form.

export interface EngineUIState {
  selected: boolean;
  voice?: string;
  emotion: number; // 0..1, mapped per-engine (exaggeration / embedding scale / alpha)
  emotionLabel?: string;
  referenceB64?: string;
  referenceName?: string;
  emotionReferenceB64?: string;
  emotionReferenceName?: string;
  /** Free-text performance note (engines with capabilities.instruction). */
  instruction?: string;
  /** Exact transcript of the reference clip — Breeze refuses to clone without it. */
  referenceText?: string;
  /** Tag-annotated passage from the director; overrides the shared text for
   *  this engine only, so "(sigh)" never reaches an engine that would read it
   *  aloud as a word. */
  directedText?: string;
  speed: number;
}

export function defaultEngineState(): EngineUIState {
  return { selected: false, emotion: 0.5, speed: 1.0 };
}
