// Mirrors the gateway/engine contract (shared/schemas.py).

export interface Capabilities {
  cloning: boolean;
  emotion_scalar: boolean;
  emotion_tags: boolean;
  emotion_reference: boolean;
  /** Takes a free-text performance note. No fixed vocabulary. */
  instruction: boolean;
  multi_speaker: boolean;
  streaming: boolean;
  requires_gpu: boolean;
  languages: string[];
}

export interface VoiceInfo {
  id: string;
  label: string;
  language: string;
}

export interface EngineInfo {
  id: string;
  name: string;
  description: string;
  license: string;
  homepage: string;
  sample_rate: number;
  capabilities: Capabilities;
  voices: VoiceInfo[];
  emotion_labels: string[];
  /** Inline events this engine understands verbatim, e.g. "(sigh)". */
  audio_tags: string[];
}

export interface EngineStatus {
  id: string;
  url: string;
  available: boolean;
  loaded: boolean;
  info: EngineInfo | null;
  error: string | null;
}

export interface BakeoffItem {
  engine_id: string;
  /** Overrides the shared text for this engine only (tag-annotated passage). */
  text?: string | null;
  voice?: string | null;
  reference_audio_b64?: string | null;
  reference_text?: string | null;
  instruction?: string | null;
  emotion?: number | null;
  emotion_label?: string | null;
  emotion_reference_b64?: string | null;
  speed?: number;
  seed?: number | null;
}

export interface BakeoffResult {
  engine_id: string;
  ok: boolean;
  audio_b64: string | null;
  meta: Record<string, number> | null;
  error: string | null;
}

export interface BakeoffResponse {
  results: BakeoffResult[];
}

export type LLMProvider = "ollama" | "gemini";

export interface DirectorSegment {
  speaker: string;
  text: string;
  emotion: string;
}

export interface DirectorEngineSpec {
  id: string;
  name: string;
  emotion_labels: string[];
  voices: string[];
  audio_tags: string[];
  emotion_scalar: boolean;
  emotion_tags: boolean;
  instruction: boolean;
}

export interface DirectorEngineDirection {
  engine_id: string;
  emotion_label: string | null;
  voice: string | null;
  scalar: number;
  /** Prose performance note; only ever set for instruction-capable engines. */
  instruction: string | null;
  /** The passage with inline tags added. Null = use the shared text as-is. */
  text: string | null;
}

export interface DirectorResult {
  summary: string;
  emotion: string;
  scalar: number;
  voice_hint: string;
  segments: DirectorSegment[];
  per_engine: DirectorEngineDirection[];
}
