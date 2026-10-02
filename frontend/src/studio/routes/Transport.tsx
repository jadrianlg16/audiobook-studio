import { useCallback, useEffect, useRef, useState } from "react";

import { fmtDuration } from "../ui/format";

export const SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2];

export interface Playhead {
  ms: number;
  playing: boolean;
  /** true when the chapter WAV could not be loaded and the clock is synthetic */
  simulated: boolean;
  rate: number;
  setRate: (rate: number) => void;
  toggle: () => void;
  seek: (ms: number) => void;
}

/**
 * Drives follow-along highlighting from the chapter WAV, exactly as the
 * read-along player does: a clock in milliseconds, matched against each line's
 * audio.start_ms / end_ms.
 *
 * When the WAV cannot be loaded (nothing rendered yet, or mock data) the clock
 * keeps running on a timer so the script still follows. That is labelled
 * loudly, not hidden: a QC pass must not sign off a chapter nobody heard.
 */
export function usePlayhead(src: string | null, totalMs: number): Playhead {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [ms, setMs] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [simulated, setSimulated] = useState(!src);
  const [rate, setRate] = useState(1);

  // (re)build the element when the chapter changes
  useEffect(() => {
    setMs(0);
    setPlaying(false);
    audioRef.current?.pause();
    if (!src) {
      audioRef.current = null;
      setSimulated(true);
      return;
    }
    const el = new Audio(src);
    el.preload = "metadata";
    const onError = () => setSimulated(true);
    const onCanPlay = () => setSimulated(false);
    const onEnded = () => setPlaying(false);
    el.addEventListener("error", onError);
    el.addEventListener("loadedmetadata", onCanPlay);
    el.addEventListener("ended", onEnded);
    audioRef.current = el;
    setSimulated(false);
    return () => {
      el.pause();
      el.removeEventListener("error", onError);
      el.removeEventListener("loadedmetadata", onCanPlay);
      el.removeEventListener("ended", onEnded);
      audioRef.current = null;
    };
  }, [src]);

  // Play/pause is a side effect, so it lives in an effect — inside the
  // setPlaying updater React 18's StrictMode double-invoked it in dev.
  useEffect(() => {
    const el = audioRef.current;
    if (!el || simulated) return;
    if (playing) {
      void el.play().catch(() => setSimulated(true));
    } else {
      el.pause();
    }
  }, [playing, simulated]);

  useEffect(() => {
    const el = audioRef.current;
    if (el) el.playbackRate = rate;
  }, [rate, src]);

  // the clock: 250 ms, not 100 — this re-renders the whole script column
  useEffect(() => {
    if (!playing) return;
    const step = 250;
    const id = window.setInterval(() => {
      const el = audioRef.current;
      if (el && !simulated && !el.paused) {
        setMs(el.currentTime * 1000);
        return;
      }
      setMs((prev) => Math.min(totalMs, prev + step * rate));
    }, step);
    return () => window.clearInterval(id);
  }, [playing, simulated, totalMs, rate]);

  useEffect(() => {
    if (playing && totalMs > 0 && ms >= totalMs) setPlaying(false);
  }, [ms, totalMs, playing]);

  const toggle = useCallback(() => setPlaying((p) => !p), []);

  const seek = useCallback(
    (target: number) => {
      const clamped = Math.max(0, Math.min(totalMs, target));
      setMs(clamped);
      const el = audioRef.current;
      if (el && !simulated) el.currentTime = clamped / 1000;
    },
    [simulated, totalMs],
  );

  return { ms, playing, simulated, rate, setRate, toggle, seek };
}

export function Transport({
  head,
  totalMs,
  follow,
  onFollowChange,
  label,
  chapterLabel,
}: {
  head: Playhead;
  totalMs: number;
  follow: boolean;
  onFollowChange: (v: boolean) => void;
  label: string;
  chapterLabel: string;
}) {
  const pctDone = totalMs > 0 ? (head.ms / totalMs) * 100 : 0;
  const step = 5000;

  return (
    <div className="transport">
      <div
        className="scrub"
        role="slider"
        tabIndex={0}
        aria-label={`${chapterLabel} position`}
        aria-valuemin={0}
        aria-valuemax={Math.round(totalMs / 1000)}
        aria-valuenow={Math.round(head.ms / 1000)}
        aria-valuetext={`${fmtDuration(head.ms)} of ${fmtDuration(totalMs)}`}
        onClick={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          head.seek(((e.clientX - r.left) / r.width) * totalMs);
        }}
        onKeyDown={(e) => {
          // Without preventDefault the arrows scrub *and* scroll the column.
          const keys = ["ArrowRight", "ArrowLeft", "Home", "End", "PageUp", "PageDown"];
          if (!keys.includes(e.key)) return;
          e.preventDefault();
          if (e.key === "ArrowRight") head.seek(head.ms + step);
          if (e.key === "ArrowLeft") head.seek(head.ms - step);
          if (e.key === "PageUp") head.seek(head.ms + step * 6);
          if (e.key === "PageDown") head.seek(head.ms - step * 6);
          if (e.key === "Home") head.seek(0);
          if (e.key === "End") head.seek(totalMs);
        }}
      >
        <div className="fill" style={{ width: `${pctDone}%` }} />
        <div className="head" style={{ left: `${pctDone}%` }} />
      </div>

      <div className="row mono" style={{ fontSize: 11, color: "var(--ink-faint)", gap: 14 }}>
        <button
          className="btn sm"
          onClick={() => head.seek(head.ms - 15000)}
          aria-label="Back 15 seconds"
        >
          ◀◀
        </button>
        <button
          className="btn sm"
          onClick={head.toggle}
          aria-label={head.playing ? "Pause" : "Play"}
        >
          {head.playing ? "❚❚" : "▶"}
        </button>
        <button
          className="btn sm"
          onClick={() => head.seek(head.ms + 15000)}
          aria-label="Forward 15 seconds"
        >
          ▶▶
        </button>
        <span>
          {fmtDuration(head.ms)} / {fmtDuration(totalMs)}
        </span>
        <label className="row" style={{ gap: 5 }}>
          <span className="sronly">Playback speed</span>
          <select
            className="speed"
            value={head.rate}
            onChange={(e) => head.setRate(Number(e.target.value))}
            title="Playback speed — a QC pass is done at 1.5×"
          >
            {SPEEDS.map((s) => (
              <option key={s} value={s}>
                {s}×
              </option>
            ))}
          </select>
        </label>
        <label className="row" style={{ gap: 5, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={follow}
            onChange={(e) => onFollowChange(e.target.checked)}
          />
          follow script
        </label>
        <span className="faint">
          {chapterLabel} · {label}
        </span>
        {head.simulated && (
          <span
            className="chip unknown"
            title="There is no chapter WAV to play. The playhead is a timer: nothing is being heard."
          >
            simulated clock — no audio
          </span>
        )}
      </div>
    </div>
  );
}
