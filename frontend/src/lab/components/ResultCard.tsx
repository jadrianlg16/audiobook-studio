import type { BakeoffResult } from "../types";

interface Props {
  name: string;
  result: BakeoffResult;
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] uppercase tracking-wide text-neutral-500">{label}</span>
      <span className="text-sm font-medium text-neutral-200">{value}</span>
    </div>
  );
}

export function ResultCard({ name, result }: Props) {
  if (!result.ok || !result.audio_b64) {
    return (
      <div className="rounded-xl border border-rose-900/60 bg-rose-950/30 p-4">
        <h3 className="font-semibold">{name}</h3>
        <p className="mt-2 text-xs text-rose-300">{result.error ?? "failed"}</p>
      </div>
    );
  }

  const src = `data:audio/wav;base64,${result.audio_b64}`;
  const meta = result.meta ?? {};
  const rtf = meta.rtf ?? 0;

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-4">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">{name}</h3>
        <a
          href={src}
          download={`${result.engine_id}.wav`}
          className="text-xs text-indigo-400 hover:text-indigo-300"
        >
          download
        </a>
      </div>

      <audio className="mt-3" controls src={src} />

      <div className="mt-3 grid grid-cols-3 gap-2">
        <Metric label="RTF" value={rtf ? `${rtf.toFixed(2)}×` : "—"} />
        <Metric label="gen" value={meta.gen_seconds ? `${meta.gen_seconds.toFixed(2)}s` : "—"} />
        <Metric
          label="audio"
          value={meta.audio_seconds ? `${meta.audio_seconds.toFixed(1)}s` : "—"}
        />
      </div>
    </div>
  );
}
