import { useEffect, useMemo, useRef, useState } from "react";

import { errorMessage } from "../studio/api/http";
import {
  annotateWithDirector,
  fetchDirectorDefaults,
  fetchEngines,
  runBakeoff,
  unloadEngine,
} from "./api";
import { EngineControls } from "./components/EngineControls";
import { ResultCard } from "./components/ResultCard";
import {
  applyDirectionToEngine,
  PRESETS,
  type AppliedDirection,
  type Direction,
  type DirectorPreset,
} from "./presets";
import { defaultEngineState, type EngineUIState } from "./state";
import type {
  BakeoffItem,
  BakeoffResult,
  DirectorResult,
  EngineStatus,
  LLMProvider,
} from "./types";

const SAMPLE = PRESETS[1].text;

// Fallbacks only. The real Ollama default comes from the gateway
// (GET /api/director/defaults, set by DIRECTOR_MODEL) so moving to a machine
// with a bigger director is an env var, not a frontend rebuild.
const DEFAULT_MODEL: Record<LLMProvider, string> = {
  ollama: "llama3.2",
  gemini: "gemini-flash-latest",
};

export default function App() {
  const [engines, setEngines] = useState<EngineStatus[]>([]);
  const [states, setStates] = useState<Record<string, EngineUIState>>({});
  const [text, setText] = useState(SAMPLE);
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const [directionNote, setDirectionNote] = useState("");
  const [directions, setDirections] = useState<AppliedDirection[]>([]);
  const [results, setResults] = useState<BakeoffResult[] | null>(null);
  const [running, setRunning] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // AI director config
  const [provider, setProvider] = useState<LLMProvider>("ollama");
  const [model, setModel] = useState(DEFAULT_MODEL.ollama);
  const [geminiKey, setGeminiKey] = useState("");
  const [analyzing, setAnalyzing] = useState(false);
  const [aiResult, setAiResult] = useState<DirectorResult | null>(null);
  const [ollamaDefault, setOllamaDefault] = useState(DEFAULT_MODEL.ollama);

  // Guards against overlapping refreshes (mount effect + ↻ button + post-unload
  // reload): if a newer loadEngines() started, an older one's response is ignored
  // so it can't clobber fresher data.
  const loadSeq = useRef(0);

  async function loadEngines() {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const list = await fetchEngines();
      if (seq !== loadSeq.current) return; // superseded by a newer refresh
      setEngines(list);
      setStates((prev) => {
        const next = { ...prev };
        for (const e of list) {
          if (!next[e.id]) {
            // Seed voice to the engine's first built-in voice so what the Voice
            // dropdown shows is actually what gets synthesized (the <select>
            // visually defaults to voices[0], but state.voice stayed undefined).
            const firstVoice = e.info?.capabilities.multi_speaker
              ? e.info.voices[0]?.id
              : undefined;
            next[e.id] = { ...defaultEngineState(), selected: e.available, voice: firstVoice };
          }
        }
        return next;
      });
      setError(null);
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setError(`Could not reach the gateway. ${errorMessage(e)}`);
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }

  useEffect(() => {
    void loadEngines();
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        const d = await fetchDirectorDefaults();
        if (!d.model) return;
        setOllamaDefault(d.model);
        // Only pre-fill while the field still holds the built-in fallback, so a
        // model the user typed is never overwritten by a late response.
        setModel((m) => (m === DEFAULT_MODEL.ollama ? d.model : m));
      } catch {
        // Older gateway or none running — the fallback above is fine.
      }
    })();
  }, []);

  // A tag-annotated passage belongs to the exact text it was written for. The
  // moment the text changes, drop it — otherwise an engine quietly synthesizes
  // an annotation of the previous paragraph.
  useEffect(() => {
    setStates((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const [id, st] of Object.entries(prev)) {
        if (st.directedText) {
          next[id] = { ...st, directedText: undefined };
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [text]);

  const nameById = useMemo(() => {
    const map: Record<string, string> = {};
    for (const e of engines) map[e.id] = e.info?.name ?? e.id;
    return map;
  }, [engines]);

  function patch(id: string, p: Partial<EngineUIState>) {
    setStates((s) => ({ ...s, [id]: { ...s[id], ...p } }));
  }

  async function handleUnload(id: string) {
    try {
      await unloadEngine(id);
      await loadEngines();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  // Shared apply path: translate one direction into every engine's controls.
  function applyDirection(
    direction: Direction,
    opts: { tag: string; note?: string; text?: string },
  ) {
    if (opts.text !== undefined) setText(opts.text);
    setActiveTag(opts.tag);
    setDirectionNote(opts.note ?? "");
    const applied: AppliedDirection[] = [];
    setStates((prev) => {
      const next = { ...prev };
      for (const e of engines) {
        const base = next[e.id] ?? defaultEngineState();
        const { state, applied: a } = applyDirectionToEngine(direction, e, base);
        next[e.id] = state;
        if (e.available) applied.push(a);
      }
      return next;
    });
    setDirections(applied);
  }

  function applyPreset(p: DirectorPreset) {
    setAiResult(null);
    applyDirection(
      { scalar: p.scalar, emotion: p.emotion },
      { tag: p.id, note: p.intent, text: p.text },
    );
  }

  // Apply the LLM's per-engine choices directly (each value is already from that
  // engine's real vocabulary, validated server-side).
  function applyPerEngine(result: DirectorResult) {
    setActiveTag("ai");
    setDirectionNote(`AI (${provider}): ${result.summary}`);
    const applied: AppliedDirection[] = [];
    setStates((prev) => {
      const next = { ...prev };
      for (const pe of result.per_engine) {
        const eng = engines.find((e) => e.id === pe.engine_id);
        if (!eng) continue;
        const caps = eng.info?.capabilities;
        const st = { ...(next[pe.engine_id] ?? defaultEngineState()) };
        const parts: string[] = [];
        if (caps?.emotion_scalar) {
          st.emotion = pe.scalar;
          parts.push(`expr ${pe.scalar.toFixed(2)}`);
        }
        if (caps?.emotion_tags) {
          // Apply the director's choice fully — including clearing a previously
          // set label when it returns null — so the dropdown never shows a stale
          // emotion the director didn't actually pick.
          st.emotionLabel = pe.emotion_label ?? undefined;
          if (pe.emotion_label) parts.push(`emotion “${pe.emotion_label}”`);
        }
        if (caps?.multi_speaker && pe.voice) {
          st.voice = pe.voice;
          parts.push(`voice ${pe.voice}`);
        }
        if (caps?.instruction) {
          // Apply fully, clearing a stale note when the director returns null.
          st.instruction = pe.instruction ?? undefined;
          if (pe.instruction) parts.push("directed");
        }
        // Only engines that advertise a tag vocabulary may receive tagged text —
        // anywhere else "(sigh)" would be spoken as a literal word.
        if ((eng.info?.audio_tags?.length ?? 0) > 0) {
          st.directedText = pe.text ?? undefined;
          if (pe.text) parts.push("tagged");
        }
        if (parts.length === 0) parts.push("neutral");
        next[pe.engine_id] = st;
        if (eng.available) applied.push({ engineId: pe.engine_id, parts });
      }
      return next;
    });
    setDirections(applied);
  }

  async function analyze() {
    setAnalyzing(true);
    setError(null);
    try {
      const specs = engines
        .filter((e) => e.available && e.info)
        .map((e) => ({
          id: e.id,
          name: e.info!.name,
          emotion_labels: e.info!.emotion_labels,
          voices: e.info!.voices.map((v) => v.id),
          audio_tags: e.info!.audio_tags ?? [],
          emotion_scalar: e.info!.capabilities.emotion_scalar,
          emotion_tags: e.info!.capabilities.emotion_tags,
          instruction: e.info!.capabilities.instruction,
        }));
      const result = await annotateWithDirector({
        text,
        provider,
        model,
        apiKey: geminiKey,
        engines: specs,
      });
      setAiResult(result);
      applyPerEngine(result);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setAnalyzing(false);
    }
  }

  function changeProvider(p: LLMProvider) {
    setProvider(p);
    setModel(p === "ollama" ? ollamaDefault : DEFAULT_MODEL[p]);
  }

  const selected = engines.filter((e) => e.available && states[e.id]?.selected);

  const runSeq = useRef(0);

  async function run() {
    const seq = ++runSeq.current;
    setRunning(true);
    setError(null);
    setResults(null);
    try {
      const items: BakeoffItem[] = selected.map((e) => {
        const st = states[e.id];
        const caps = e.info?.capabilities;
        const hasTags = (e.info?.audio_tags?.length ?? 0) > 0;
        return {
          engine_id: e.id,
          // Per-engine override; undefined falls back to the shared text.
          text: hasTags ? st.directedText : undefined,
          voice: caps?.multi_speaker ? st.voice : undefined,
          emotion: caps?.emotion_scalar ? st.emotion : undefined,
          emotion_label: caps?.emotion_tags ? st.emotionLabel : undefined,
          instruction: caps?.instruction ? st.instruction : undefined,
          reference_audio_b64: caps?.cloning ? st.referenceB64 : undefined,
          reference_text: caps?.cloning ? st.referenceText : undefined,
          emotion_reference_b64: caps?.emotion_reference ? st.emotionReferenceB64 : undefined,
          speed: st.speed,
        };
      });
      const resp = await runBakeoff(text, items);
      if (seq !== runSeq.current) return; // a newer bake-off superseded this one
      setResults(resp.results);
    } catch (e) {
      if (seq !== runSeq.current) return;
      setError(errorMessage(e));
    } finally {
      if (seq === runSeq.current) setRunning(false);
    }
  }

  const inputCls =
    "rounded-md bg-neutral-800 border border-neutral-700 px-2 py-1.5 text-sm focus:outline-none focus:border-indigo-500";

  return (
    <div className="mx-auto max-w-6xl px-5 py-8">
      <header className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Audiobook Studio</h1>
          <p className="text-sm text-neutral-400">
            Voice engine bake-off — same text, every engine, side by side.
          </p>
        </div>
        <button
          onClick={() => void loadEngines()}
          className="rounded-md border border-neutral-700 px-3 py-1.5 text-sm hover:bg-neutral-800"
        >
          ↻ Engines
        </button>
      </header>

      {/* Director */}
      <section className="mb-5 rounded-xl border border-neutral-800 bg-neutral-900/40 p-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-neutral-400">
            🎬 Director
          </h2>
          <span className="text-xs text-neutral-500">
            one intent → each engine’s native controls
          </span>
        </div>

        {/* Quick presets */}
        <div className="mt-3 flex flex-wrap gap-2">
          {PRESETS.map((p) => (
            <button
              key={p.id}
              onClick={() => applyPreset(p)}
              className={
                "rounded-lg border px-3 py-1.5 text-sm transition " +
                (activeTag === p.id
                  ? "border-indigo-500 bg-indigo-600/20 text-indigo-200"
                  : "border-neutral-700 hover:bg-neutral-800")
              }
            >
              {p.name}
            </button>
          ))}
        </div>

        {/* AI director config */}
        <div className="mt-4 rounded-lg border border-neutral-800 bg-neutral-950/40 p-3">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-neutral-400">LLM provider</span>
              <select
                className={inputCls}
                value={provider}
                onChange={(e) => changeProvider(e.target.value as LLMProvider)}
              >
                <option value="ollama">Ollama (local)</option>
                <option value="gemini">Gemini API</option>
              </select>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-xs text-neutral-400">Model</span>
              <input
                className={`${inputCls} w-48`}
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder={DEFAULT_MODEL[provider]}
              />
            </label>

            {provider === "gemini" && (
              <label className="flex flex-col gap-1">
                <span className="text-xs text-neutral-400">Gemini API key</span>
                <input
                  type="password"
                  className={`${inputCls} w-56`}
                  value={geminiKey}
                  onChange={(e) => setGeminiKey(e.target.value)}
                  placeholder="AIza…"
                  autoComplete="off"
                />
              </label>
            )}

            <button
              onClick={() => void analyze()}
              disabled={analyzing || !model.trim() || (provider === "gemini" && !geminiKey)}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-neutral-700"
            >
              {analyzing ? "Analyzing…" : "Analyze text ▶"}
            </button>
          </div>
          <p className="mt-2 text-xs text-neutral-500">
            {provider === "ollama"
              ? `Reads the text on your local Ollama (must be running). Default comes from the gateway's DIRECTOR_MODEL (${ollamaDefault}).`
              : "Free tier default gemini-flash-latest (Gemini 3 Flash). Your key is used per-request, not stored."}
          </p>
        </div>

        {/* Applied direction + AI analysis */}
        {activeTag && (
          <div className="mt-3 space-y-2">
            {directionNote && <p className="text-xs italic text-neutral-400">{directionNote}</p>}
            {directions.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {directions.map((d) => (
                  <span
                    key={d.engineId}
                    className="rounded bg-neutral-800 px-2 py-0.5 text-[11px] text-neutral-300"
                  >
                    <b className="text-neutral-100">{nameById[d.engineId] ?? d.engineId}</b>
                    {" · "}
                    {d.parts.join(", ")}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}

        {aiResult && (
          <div className="mt-3 rounded-lg border border-indigo-900/50 bg-indigo-950/20 p-3 text-sm">
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-neutral-300">
              <span>
                emotion: <b className="text-indigo-200">{aiResult.emotion}</b>
              </span>
              <span>
                expressiveness: <b className="text-indigo-200">{aiResult.scalar.toFixed(2)}</b>
              </span>
              {aiResult.voice_hint && (
                <span>
                  voice: <i>{aiResult.voice_hint}</i>
                </span>
              )}
            </div>
            {aiResult.segments.length > 0 && (
              <ul className="mt-2 space-y-1 text-xs text-neutral-400">
                {aiResult.segments.slice(0, 8).map((s, i) => (
                  <li key={i}>
                    <b className="text-neutral-200">{s.speaker}</b>
                    <span className="text-indigo-300/80"> [{s.emotion}]</span> — {s.text}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </section>

      <section className="mb-6">
        <label className="text-xs font-medium text-neutral-400">Text</label>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={5}
          className="mt-1 w-full resize-y rounded-lg border border-neutral-800 bg-neutral-900 p-3 text-sm leading-relaxed focus:border-indigo-500 focus:outline-none"
        />
      </section>

      <section className="mb-6">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-neutral-400">
            Engines
          </h2>
          {loading && <span className="text-xs text-neutral-500">discovering…</span>}
        </div>

        {!loading && engines.length === 0 && (
          <p className="rounded-lg border border-neutral-800 bg-neutral-900 p-4 text-sm text-neutral-400">
            No engines registered. Is the gateway running?
          </p>
        )}

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {engines.map((e) => (
            <EngineControls
              key={e.id}
              status={e}
              state={states[e.id] ?? defaultEngineState()}
              onChange={(p) => patch(e.id, p)}
              onUnload={() => void handleUnload(e.id)}
            />
          ))}
        </div>
      </section>

      <div className="sticky bottom-4 z-10 mb-8 flex items-center gap-3">
        <button
          onClick={() => void run()}
          disabled={running || selected.length === 0}
          className="rounded-lg bg-indigo-600 px-5 py-2.5 font-semibold text-white shadow-lg shadow-indigo-900/30 transition hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-neutral-700"
        >
          {running ? "Synthesizing…" : `Run bake-off (${selected.length})`}
        </button>
        {error && <span className="text-sm text-rose-400">{error}</span>}
      </div>

      {results && (
        <section>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-neutral-400">
            Results
          </h2>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {results.map((r) => (
              <ResultCard
                key={r.engine_id}
                name={nameById[r.engine_id] ?? r.engine_id}
                result={r}
              />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
