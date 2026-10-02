import { http, jsonBody } from "../studio/api/http";
import type {
  BakeoffItem,
  BakeoffResponse,
  DirectorEngineSpec,
  DirectorResult,
  EngineStatus,
  LLMProvider,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "/api";

// Every call goes through the studio's http(): a failed one throws an ApiError
// with the status, the gateway's detail and the URL.

export function fetchEngines(): Promise<EngineStatus[]> {
  return http<EngineStatus[]>(`${API_BASE}/engines`);
}

export function runBakeoff(text: string, items: BakeoffItem[]): Promise<BakeoffResponse> {
  return http<BakeoffResponse>(`${API_BASE}/bakeoff`, jsonBody("POST", { text, items }));
}

/** The director model this deployment prefers. Keeps the machine-specific
 *  choice in the gateway's env instead of baked into the bundle. */
export function fetchDirectorDefaults(): Promise<{ provider: string; model: string }> {
  return http(`${API_BASE}/director/defaults`);
}

/** Free an engine's model from VRAM (reloads lazily on next synth). */
export async function unloadEngine(engineId: string): Promise<void> {
  await http<unknown>(`${API_BASE}/unload/${engineId}`, { method: "POST" });
}

/** Ask the LLM director (Ollama or Gemini) to analyze text into direction. */
export async function annotateWithDirector(args: {
  text: string;
  provider: LLMProvider;
  model: string;
  apiKey?: string;
  engines: DirectorEngineSpec[];
}): Promise<DirectorResult> {
  return http<DirectorResult>(
    `${API_BASE}/director/annotate`,
    jsonBody("POST", {
      text: args.text,
      provider: args.provider,
      model: args.model,
      api_key: args.apiKey || null,
      engines: args.engines,
    }),
  );
}

/** Read a File into a bare base64 string (no data: prefix). */
export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      resolve(result.split(",")[1] ?? "");
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
