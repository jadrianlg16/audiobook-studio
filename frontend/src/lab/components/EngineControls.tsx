import { fileToBase64 } from "../api";
import type { EngineUIState } from "../state";
import type { EngineStatus } from "../types";

interface Props {
  status: EngineStatus;
  state: EngineUIState;
  onChange: (patch: Partial<EngineUIState>) => void;
  onUnload: () => void;
}

const labelCls = "text-xs font-medium text-neutral-400";
const inputCls =
  "w-full rounded-md bg-neutral-800 border border-neutral-700 px-2 py-1.5 text-sm " +
  "focus:outline-none focus:border-indigo-500";

export function EngineControls({ status, state, onChange, onUnload }: Props) {
  const info = status.info;
  const caps = info?.capabilities;
  const disabled = !status.available;
  const directable = Boolean(caps?.emotion_scalar || caps?.emotion_tags || caps?.instruction);
  const lazyPending = status.available && !status.loaded && Boolean(caps?.requires_gpu);

  async function uploadRef(
    e: React.ChangeEvent<HTMLInputElement>,
    field: "reference" | "emotionReference",
  ) {
    const file = e.target.files?.[0];
    if (!file) return;
    const b64 = await fileToBase64(file);
    if (field === "reference") {
      onChange({ referenceB64: b64, referenceName: file.name });
    } else {
      onChange({ emotionReferenceB64: b64, emotionReferenceName: file.name });
    }
  }

  return (
    <div
      className={
        "rounded-xl border p-4 transition " +
        (state.selected && !disabled
          ? "border-indigo-600/60 bg-neutral-900"
          : "border-neutral-800 bg-neutral-900/40") +
        (disabled ? " opacity-60" : "")
      }
    >
      <header className="flex items-start justify-between gap-2">
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            className="h-4 w-4 accent-indigo-500"
            checked={state.selected}
            disabled={disabled}
            onChange={(e) => onChange({ selected: e.target.checked })}
          />
          <span className="font-semibold">{info?.name ?? status.id}</span>
        </label>
        <div className="flex flex-wrap justify-end gap-1">
          {directable && (
            <span className="rounded bg-indigo-900/40 px-1.5 py-0.5 text-[10px] text-indigo-300">
              🎬 directable
            </span>
          )}
          {info && (
            <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] text-neutral-400">
              {info.license}
            </span>
          )}
          {caps?.requires_gpu && (
            <span className="rounded bg-amber-900/40 px-1.5 py-0.5 text-[10px] text-amber-300">
              GPU
            </span>
          )}
        </div>
      </header>

      {info && <p className="mt-2 text-xs leading-relaxed text-neutral-400">{info.description}</p>}
      {lazyPending && (
        <p className="mt-2 text-xs text-amber-400/80">loads on first run (~10–30s)</p>
      )}
      {status.loaded && caps?.requires_gpu && (
        <div className="mt-2 flex items-center gap-2 text-xs">
          <span className="text-emerald-400">● model loaded (VRAM)</span>
          <button
            onClick={onUnload}
            className="rounded border border-neutral-700 px-1.5 py-0.5 text-neutral-300 hover:bg-neutral-800"
          >
            ⏏ free VRAM
          </button>
        </div>
      )}
      {disabled && (
        <p className="mt-2 text-xs text-rose-400">
          unavailable{status.error ? `: ${status.error}` : ""}
        </p>
      )}

      {!disabled && info && (
        <div className="mt-3 space-y-3">
          {caps?.multi_speaker && info.voices.length > 0 && (
            <div>
              <span className={labelCls}>Voice</span>
              <select
                className={inputCls}
                value={state.voice ?? info.voices[0].id}
                onChange={(e) => onChange({ voice: e.target.value })}
              >
                {info.voices.map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.label}
                  </option>
                ))}
              </select>
            </div>
          )}

          {caps?.emotion_scalar && (
            <div>
              <span className={labelCls}>Expressiveness · {state.emotion.toFixed(2)}</span>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={state.emotion}
                className="w-full accent-indigo-500"
                onChange={(e) => onChange({ emotion: Number(e.target.value) })}
              />
            </div>
          )}

          {caps?.emotion_tags && info.emotion_labels.length > 0 && (
            <div>
              <span className={labelCls}>Emotion</span>
              <select
                className={inputCls}
                value={state.emotionLabel ?? ""}
                onChange={(e) => onChange({ emotionLabel: e.target.value || undefined })}
              >
                <option value="">— none —</option>
                {info.emotion_labels.map((lbl) => (
                  <option key={lbl} value={lbl}>
                    {lbl}
                  </option>
                ))}
              </select>
            </div>
          )}

          {caps?.instruction && (
            <div>
              <span className={labelCls}>Direction (free text)</span>
              <textarea
                rows={2}
                className={`${inputCls} resize-y`}
                value={state.instruction ?? ""}
                placeholder="Speak slowly, restrained, as if afraid of being overheard."
                onChange={(e) => onChange({ instruction: e.target.value })}
              />
              {info.audio_tags.length > 0 && (
                <p className="mt-1 text-[10px] leading-relaxed text-neutral-500">
                  Inline events: {info.audio_tags.slice(0, 8).join(" ")}
                  {info.audio_tags.length > 8 && " …"}
                </p>
              )}
              {state.directedText && (
                <p className="mt-1 text-[10px] text-indigo-300">
                  🎬 director tagged this passage — this engine gets the annotated text
                </p>
              )}
            </div>
          )}

          {caps?.cloning && (
            <div>
              <span className={labelCls}>Reference voice (clone)</span>
              <input
                type="file"
                accept="audio/*"
                className="block w-full text-xs text-neutral-400 file:mr-2 file:rounded file:border-0 file:bg-neutral-800 file:px-2 file:py-1 file:text-neutral-200"
                onChange={(e) => uploadRef(e, "reference")}
              />
              {state.referenceName && (
                <span className="text-[10px] text-emerald-400">{state.referenceName}</span>
              )}
              {caps?.instruction && (
                <>
                  <input
                    className={`${inputCls} mt-1`}
                    value={state.referenceText ?? ""}
                    placeholder="Exact transcript of that clip"
                    onChange={(e) => onChange({ referenceText: e.target.value })}
                  />
                  {state.referenceB64 && !state.referenceText?.trim() && (
                    <span className="text-[10px] text-amber-400">
                      required — this engine clones from audio + its transcript
                    </span>
                  )}
                </>
              )}
            </div>
          )}

          {caps?.emotion_reference && (
            <div>
              <span className={labelCls}>Emotion reference clip</span>
              <input
                type="file"
                accept="audio/*"
                className="block w-full text-xs text-neutral-400 file:mr-2 file:rounded file:border-0 file:bg-neutral-800 file:px-2 file:py-1 file:text-neutral-200"
                onChange={(e) => uploadRef(e, "emotionReference")}
              />
              {state.emotionReferenceName && (
                <span className="text-[10px] text-emerald-400">{state.emotionReferenceName}</span>
              )}
            </div>
          )}

          <div>
            <span className={labelCls}>Speed · {state.speed.toFixed(2)}×</span>
            <input
              type="range"
              min={0.5}
              max={2}
              step={0.05}
              value={state.speed}
              className="w-full accent-indigo-500"
              onChange={(e) => onChange({ speed: Number(e.target.value) })}
            />
          </div>
        </div>
      )}
    </div>
  );
}
