import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useApi } from "../api/client";
import { errorMessage } from "../api/http";
import type { AcxFile, BookSummary, Job, JobLogEntry, JobProgress } from "../api/types";
import { isTerminalJob } from "../domain";
import { ErrorBox, ProgressBar, Spinner } from "../ui/bits";
import { fmtBytes, fmtDuration, fmtSeconds, pct } from "../ui/format";
import { SignOffSummary } from "../ui/signoff";
import { TopBar } from "../ui/TopBar";

const KIND_VERB: Record<Job["kind"], string> = {
  render: "Rendering",
  direct: "Directing",
  export: "Exporting",
};

/** What "can I close this" actually means, per kind. */
const KIND_RESUME: Record<Job["kind"], string> = {
  render: "resumable — the cache makes a resume cheap",
  direct: "resumable — decided scenes are kept",
  export: "not checkpointed — a resumed export re-masters from the chapter WAVs",
};

/**
 * The job screen. It answers the two questions a multi-hour render
 * actually raises: how long, and can I close this.
 */
export function JobScreen() {
  const api = useApi();
  const navigate = useNavigate();
  const { id = "" } = useParams();

  const [job, setJob] = useState<JobProgress | null>(null);
  const [logs, setLogs] = useState<JobLogEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  /** How updates arrive: the live stream, a reconnect in progress, or polling. */
  const [link, setLink] = useState<"live" | "reconnecting" | "polling">("live");
  /** Set when the subscription gave up for good (see jobStream.ts); Retry opens a new one. */
  const [stopped, setStopped] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  /**
   * Bumped to open a new subscription: when a resumed job leaves a terminal
   * state, and when a person presses Retry after the stream gave up.
   */
  const [subscription, setSubscription] = useState(0);
  /**
   * The job record carries a slug, not a title, and no sign-off counts.
   * `GET /books/{slug}` is exactly that summary — the whole script would be
   * megabytes to read two fields off it.
   */
  const [book, setBook] = useState<BookSummary | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  /**
   * The stream is the only source here. There used to be a parallel getJob()
   * whose response could land after the first events and replace the log with
   * an older snapshot. Every (re)connect opens with a full snapshot, which
   * replaces the job outright.
   */
  useEffect(() => {
    setError(null);
    setStopped(null);
    setLink("live");
    const stop = api.subscribeJob(id, {
      onProgress: (j) => setJob(j),
      onLog: (entry) =>
        setLogs((prev) =>
          prev.some((p) => p.ts === entry.ts && p.line === entry.line)
            ? prev
            : [...prev.slice(-199), entry],
        ),
      onDone: (j) => setJob(j),
      onError: (m) => setError(m),
      onReconnecting: () => setLink("reconnecting"),
      onLive: () => setLink("live"),
      onFallback: () => setLink("polling"),
      onGiveUp: (m) => {
        // The give-up message carries the last error; one box says it.
        setError(null);
        setStopped(m);
      },
    });
    return stop;
  }, [api, id, subscription]);

  const retry = () => setSubscription((n) => n + 1);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [logs]);

  // Re-read the summary when the job reaches a terminal state: a finished
  // render resets every chapter it touched back to "unheard".
  const slug = job?.book ?? null;
  const status = job?.status ?? null;
  useEffect(() => {
    if (!slug) return;
    const ctrl = new AbortController();
    api
      .getBook(slug, ctrl.signal)
      .then((b) => {
        if (!ctrl.signal.aborted) setBook(b);
      })
      // The title and the sign-off count are a garnish on a job screen.
      .catch(() => undefined);
    return () => ctrl.abort();
  }, [api, slug, status]);

  async function control(what: "pause" | "resume" | "cancel") {
    setBusy(true);
    try {
      const next =
        what === "pause"
          ? await api.pauseJob(id)
          : what === "resume"
            ? await api.resumeJob(id)
            : await api.cancelJob(id);
      const { log: _log, ...rest } = next;
      setJob(rest);
      // A finished stream is closed for good. Resuming a failed or interrupted
      // job puts it back to work, so the screen needs a fresh subscription or it
      // sits on this one snapshot forever.
      if (what === "resume" && !isTerminalJob(next.status)) {
        setSubscription((n) => n + 1);
      }
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const blocking = stopped ?? error;
  if (blocking && !job) {
    return (
      <>
        <TopBar crumbs={<span className="mono">{id}</span>} />
        <div className="page">
          <ErrorBox error={blocking} onRetry={stopped ? retry : undefined} />
          <p style={{ marginTop: 12 }}>
            <Link className="btn" to="/">
              Back to the shelf
            </Link>
          </p>
        </div>
      </>
    );
  }
  if (!job) {
    return (
      <>
        <TopBar crumbs={<span className="mono">{id}</span>} />
        <div className="page">
          <Spinner label="attaching to the job" />
        </div>
      </>
    );
  }

  const p = job.progress;
  const done = job.status === "done";
  const percent = pct(p.done, p.total);
  const hitRate = p.done > 0 ? Math.round((p.cache_hits / p.done) * 100) : 0;
  const running = job.status === "running";
  const result = job.result as Record<string, unknown> | null;
  const unit = job.kind === "direct" ? "scenes" : job.kind === "export" ? "%" : "lines";
  const acxFiles = Array.isArray((result as { files?: AcxFile[] } | null)?.files)
    ? ((result as { files: AcxFile[] }).files ?? [])
    : null;

  return (
    <>
      <TopBar
        crumbs={
          <>
            <span>{KIND_VERB[job.kind]}</span>
            <span className="faint">/</span>
            <Link to={`/books/${job.book}`} title={job.book}>
              {book?.title ?? job.book}
            </Link>
            <span className="faint">/</span>
            <span className="mono faint">{job.id}</span>
          </>
        }
      >
        {book?.qc && (
          <span
            className="chip"
            title="Chapters a person has heard and approved, out of the chapters in the audio. A render resets the chapters it changes."
          >
            sign-off <SignOffSummary qc={book.qc} />
          </span>
        )}
        <span className={`chip ${running ? "signal" : ""}`}>{job.status}</span>
        {running ? (
          <button className="btn sm" disabled={busy} onClick={() => void control("pause")}>
            Pause
          </button>
        ) : (
          <button
            className="btn sm"
            disabled={busy || done || job.status === "cancelled"}
            onClick={() => void control("resume")}
          >
            Resume
          </button>
        )}
        <button
          className="btn sm danger"
          disabled={busy || done || job.status === "cancelled"}
          onClick={() => void control("cancel")}
        >
          Cancel
        </button>
        <button
          className="btn sm"
          title="Go back. The job keeps running."
          onClick={() => navigate(`/books/${job.book}`)}
        >
          Detach
        </button>
      </TopBar>

      <div className="page wide">
        <div className="col" style={{ gap: 14 }}>
          {stopped && <ErrorBox error={`Live updates stopped: ${stopped}`} onRetry={retry} />}
          {error && <ErrorBox error={error} />}

          <ProgressBar
            done={p.done}
            total={p.total}
            finished={done}
            label={`${KIND_VERB[job.kind]} ${job.book}: ${p.done} of ${p.total} ${unit}`}
          />

          <div className="stats" aria-live="polite">
            <span>
              {job.kind === "export"
                ? `${percent}%`
                : `${p.done.toLocaleString()} / ${p.total.toLocaleString()} ${unit} (${percent}%)`}
            </span>
            <span>cache hits {hitRate}%</span>
            <span>RTF {p.rtf ?? "–"}</span>
            <span>elapsed {fmtSeconds(p.elapsed_s)}</span>
            <span>ETA {done ? "–" : p.eta_s != null ? `~${fmtSeconds(p.eta_s)}` : "–"}</span>
            <span style={{ color: "var(--signal)" }}>{KIND_RESUME[job.kind]}</span>
            {link === "reconnecting" && (
              <span className="faint">live stream dropped, reconnecting…</span>
            )}
            {link === "polling" && (
              <span className="faint">live stream unavailable, polling every 2s</span>
            )}
          </div>

          {job.error && <ErrorBox error={job.error} />}

          {done && result && (
            <div className="notice row" style={{ justifyContent: "space-between" }}>
              {job.kind === "direct" && typeof result.pass_id === "string" && (
                <>
                  <span>Pass {String(result.pass_id)} is ready to review.</span>
                  <Link
                    className="btn sm signal"
                    to={`/books/${job.book}/review/${result.pass_id}`}
                  >
                    Review the pass
                  </Link>
                </>
              )}
              {job.kind === "export" && !acxFiles && typeof result.url === "string" && (
                <>
                  <span className="mono">
                    {String(result.file)} · {fmtBytes(Number(result.bytes ?? 0))} ·{" "}
                    {String(result.loudness ?? "")}
                  </span>
                  <a className="btn sm signal" href={String(result.url)} download>
                    Download
                  </a>
                </>
              )}
              {job.kind === "export" && acxFiles && (
                <span className="col" style={{ gap: 4, width: "100%" }}>
                  <span>
                    ACX export · {acxFiles.length} file{acxFiles.length === 1 ? "" : "s"} ·
                    compliance {String(result.compliance ?? "–")}
                  </span>
                  {acxFiles.map((f) => (
                    <span key={f.url} className="panelrow">
                      <span className="grow">{f.file}</span>
                      <span className="faint">ch {f.chapter + 1}</span>
                      <span className="faint">{fmtDuration(f.duration_ms)}</span>
                      <span className="faint">{fmtBytes(f.bytes)}</span>
                      <span className="faint">
                        {f.measured.integrated_lufs.toFixed(1)} LUFS ·{" "}
                        {f.measured.true_peak_dbtp.toFixed(1)} dBTP
                      </span>
                      <span className="faint">
                        {f.checks.peak_ok && f.checks.loudness_ok && f.checks.duration_ok
                          ? "checks pass"
                          : "checks FAIL"}
                      </span>
                      <a className="btn sm" href={f.url} download>
                        Download
                      </a>
                    </span>
                  ))}
                </span>
              )}
              {job.kind === "render" && (
                <>
                  <span>
                    {Array.isArray(result.chapters_written)
                      ? `${(result.chapters_written as number[]).length} chapters written`
                      : "Render finished"}
                    {typeof result.duration_ms === "number"
                      ? ` · ${fmtDuration(result.duration_ms)}`
                      : ""}
                  </span>
                  <Link className="btn sm signal" to={`/books/${job.book}`}>
                    Open the desk
                  </Link>
                </>
              )}
            </div>
          )}

          <div className="tablewrap">
            <table className="sheet">
              <thead>
                <tr>
                  <th style={{ width: 220 }}>Chapter</th>
                  <th>Progress</th>
                  <th style={{ width: 220 }}>Engine split</th>
                  <th style={{ width: 90 }}>Time</th>
                </tr>
              </thead>
              <tbody>
                {job.chapters.map((ch) => (
                  <tr key={`${ch.index}-${ch.title}`}>
                    <td className="mono" style={{ fontSize: 12.5 }}>
                      {/* Chapter indices are 0-based; the number a human wants
                          is index + 1, including for the first chapter. */}
                      {job.kind === "render" || job.kind === "export" ? `${ch.index + 1}  ` : ""}
                      {ch.title}
                    </td>
                    <td>
                      <ProgressBar
                        thin
                        done={ch.done}
                        total={ch.total}
                        finished={ch.status === "done"}
                        label={`${ch.title}: ${ch.done} of ${ch.total}`}
                      />
                      <span className="mono faint" style={{ fontSize: 10.5 }}>
                        {ch.done}/{ch.total}
                      </span>
                    </td>
                    <td className="mono faint" style={{ fontSize: 11 }}>
                      {Object.keys(ch.engines).length === 0
                        ? "—"
                        : Object.entries(ch.engines)
                            .map(([e, n]) => `${e} ${n}`)
                            .join("  ")}
                    </td>
                    <td
                      className="mono"
                      style={{
                        fontSize: 11,
                        color: ch.status === "running" ? "var(--signal)" : "var(--ink-faint)",
                      }}
                    >
                      {ch.status === "done"
                        ? fmtSeconds(ch.seconds)
                        : ch.status === "running"
                          ? "running"
                          : ch.status}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div>
            <span className="eyebrow">Log</span>
            <div className="logtail" ref={logRef}>
              {logs.length === 0 && <div className="faint">no output yet</div>}
              {logs.map((l, i) => (
                <div key={`${l.ts}-${i}`}>
                  <span className="faint">{new Date(l.ts).toLocaleTimeString()} </span>
                  {l.line}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
