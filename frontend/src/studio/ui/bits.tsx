import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Dispatch, ReactNode, SetStateAction } from "react";

import { errorMessage } from "../api/http";
import type { AudioState, EngineOption } from "../api/types";

export function StateDot({ state }: { state: AudioState }) {
  return <span className={`dot ${state}`} aria-hidden />;
}

export function StateLabel({ state }: { state: AudioState }) {
  return (
    <span className={`row mono state-${state}`} style={{ gap: 6, flexWrap: "nowrap" }}>
      <StateDot state={state} />
      {state}
    </span>
  );
}

/**
 * The three audio counts. The dots alone read as "19 0 0" to a screen reader,
 * so the numbers carry their own labels.
 */
export function AudioCounts({
  audio,
}: {
  audio: { cached: number; stale: number; missing: number };
}) {
  return (
    <span className="col" style={{ gap: 2 }}>
      <span className="sronly">
        cached {audio.cached}, stale {audio.stale}, missing {audio.missing}
      </span>
      <span className="row" style={{ gap: 5, flexWrap: "nowrap" }} aria-hidden>
        <StateDot state="cached" /> {audio.cached}
      </span>
      <span className="row" style={{ gap: 5, flexWrap: "nowrap" }} aria-hidden>
        <StateDot state="stale" />
        <span className={audio.stale ? "state-stale" : "faint"}>{audio.stale}</span>
      </span>
      <span className="row" style={{ gap: 5, flexWrap: "nowrap" }} aria-hidden>
        <StateDot state="missing" /> <span className="faint">{audio.missing}</span>
      </span>
    </span>
  );
}

/**
 * Engine name plus, for a non-commercial engine, the licence badge — and
 * "(not running)" when its container is down, which is the same wording the
 * inspector, the cast sheet and settings use for the same fact.
 */
export function EngineChip({
  engine,
  count,
  option,
  licenceNote,
}: {
  engine: string;
  count?: number;
  option?: EngineOption;
  licenceNote?: string | null;
}) {
  const nc = option?.non_commercial ?? false;
  const down = option !== undefined && !option.available;
  return (
    <span className="chip">
      {engine}
      {count !== undefined && <span className="faint">{count}</span>}
      {down && (
        <span
          className="faint"
          title={`${engine}'s container is not answering. Lines stay routed to it; a render fails until it is up.`}
        >
          (not running)
        </span>
      )}
      {nc && (
        <span
          className="badge-nc"
          title={
            licenceNote ??
            `${option?.license ?? "Non-commercial"} — covers self-hosted output. Personal use only.`
          }
        >
          NC
        </span>
      )}
    </span>
  );
}

/** A progress bar a screen reader can read. */
export function ProgressBar({
  done,
  total,
  label,
  thin,
  finished,
}: {
  done: number;
  total: number;
  label: string;
  thin?: boolean;
  finished?: boolean;
}) {
  const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return (
    <div
      className={`bar${thin ? " thin" : ""}`}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={total}
      aria-valuenow={done}
      aria-valuetext={`${done} of ${total} (${percent}%)`}
    >
      <div className={`fill ${finished ? "done" : ""}`} style={{ width: `${percent}%` }} />
    </div>
  );
}

export function Spinner({ label = "loading" }: { label?: string }) {
  return (
    <p className="mono faint" style={{ padding: "24px 0" }}>
      {label}…
    </p>
  );
}

export function ErrorBox({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="notice risk row" role="alert">
      <span className="mono">{error}</span>
      {onRetry && (
        <button className="btn sm" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

/** An abort we asked for, rather than a failure worth showing. */
export function isAbort(e: unknown): boolean {
  return (
    (e instanceof DOMException && e.name === "AbortError") ||
    (e instanceof Error && e.name === "AbortError")
  );
}

/**
 * Small async loader with a manual reload, used by every route.
 *
 * The loader is handed an `AbortSignal` and every API read accepts one, so
 * leaving a screen (or switching book) aborts the request in flight instead of
 * only discarding its answer. An aborted request is not an error: it sets
 * nothing, and in particular does not leave `loading` true for a screen that no
 * longer exists.
 */
export function useAsync<T>(
  fn: (signal: AbortSignal) => Promise<T>,
  deps: unknown[],
): {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
  set: Dispatch<SetStateAction<T | null>>;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const seq = useRef(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    const mine = ++seq.current;
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    fnRef
      .current(ctrl.signal)
      .then((v) => {
        if (mine !== seq.current || ctrl.signal.aborted) return;
        setData(v);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (mine !== seq.current || ctrl.signal.aborted || isAbort(e)) return;
        setError(errorMessage(e));
        setLoading(false);
      });
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload, set: setData };
}

/**
 * Confirm-once control for an irreversible action. The previous version armed
 * on the first click and disarmed after four seconds, so a second click a beat
 * later silently re-armed instead of confirming. This one stays armed until it
 * is answered, and says what it is about to delete.
 */
export function ConfirmButton({
  children,
  confirmLabel,
  question,
  onConfirm,
  className = "btn sm danger",
  disabled,
}: {
  children: ReactNode;
  confirmLabel: string;
  /** Shown next to the confirm/cancel pair, e.g. `Delete “The Cold Lantern”?` */
  question?: string;
  onConfirm: () => void;
  className?: string;
  disabled?: boolean;
}) {
  const [armed, setArmed] = useState(false);
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (armed) confirmRef.current?.focus();
  }, [armed]);

  if (!armed) {
    return (
      <button
        className={className}
        disabled={disabled}
        onClick={(e) => {
          e.stopPropagation();
          e.preventDefault();
          setArmed(true);
        }}
      >
        {children}
      </button>
    );
  }

  return (
    <span className="confirm" role="group" aria-label={question ?? "Confirm"}>
      {question && <span>{question}</span>}
      <button
        ref={confirmRef}
        className={`${className} armed`}
        disabled={disabled}
        onClick={(e) => {
          e.stopPropagation();
          e.preventDefault();
          setArmed(false);
          onConfirm();
        }}
      >
        {confirmLabel}
      </button>
      <button
        className="btn sm ghost"
        onClick={(e) => {
          e.stopPropagation();
          e.preventDefault();
          setArmed(false);
        }}
      >
        Cancel
      </button>
    </span>
  );
}

/** True while the user has asked the OS for less motion. */
export function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true
  );
}

/** scrollIntoView, but not smoothly if the user asked for less motion. */
export function scrollIntoViewSafely(el: Element | null | undefined) {
  el?.scrollIntoView({ block: "nearest", behavior: prefersReducedMotion() ? "auto" : "smooth" });
}

/**
 * Whether a scroll container is actually clipping content sideways, so the UI
 * can say so instead of hiding a whole column of buttons off-screen.
 */
export function useOverflowHint<T extends HTMLElement>(): [React.RefObject<T>, boolean] {
  const ref = useRef<T>(null);
  const [overflowing, setOverflowing] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const check = () => setOverflowing(el.scrollWidth > el.clientWidth + 1);
    check();
    // The container's own size and the content's width both decide this.
    const ro = new ResizeObserver(check);
    ro.observe(el);
    if (el.firstElementChild) ro.observe(el.firstElementChild);
    return () => ro.disconnect();
  }, []);

  return [ref, overflowing];
}
