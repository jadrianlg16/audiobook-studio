import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useApi } from "../api/client";
import { errorMessage } from "../api/http";
import type { JobStatus, JobSummary } from "../api/types";
import { isLiveJob, isResumableJob } from "../domain";
import { ErrorBox, ProgressBar, Spinner, useAsync, useOverflowHint } from "../ui/bits";
import { fmtAgo, fmtSeconds } from "../ui/format";
import { TopBar } from "../ui/TopBar";

const KIND_LABEL: Record<JobSummary["kind"], string> = {
  render: "render",
  direct: "direct",
  export: "export",
};

const UNIT: Record<JobSummary["kind"], string> = {
  render: "lines",
  direct: "scenes",
  export: "%",
};

const STATUS_TITLE: Record<JobStatus, string> = {
  queued: "Waiting for the one running job to finish.",
  running: "Working now.",
  paused: "Paused between lines. Resume picks up from the checkpoint.",
  done: "Finished.",
  failed: "Stopped on an error. Resume re-queues it; the cache makes that cheap.",
  cancelled: "Cancelled by hand.",
  interrupted: "The service restarted mid-job. Resume re-queues it from the cache.",
};

const POLL_MS = 4000;

/**
 * Every job the studio knows about, newest first.
 *
 * `GET /jobs` was in the contract and in the client, and nothing called it: a
 * render started from the shelf and then navigated away from could only be
 * found again by remembering its id. This is the way back to it, and the only
 * screen that shows work across books.
 */
export function JobsScreen() {
  const api = useApi();
  const navigate = useNavigate();
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [wrapRef, overflowing] = useOverflowHint<HTMLDivElement>();

  const load = useAsync(
    async (signal) => {
      const [jobs, books] = await Promise.all([
        api.listJobs(signal),
        // Jobs carry a slug; the shelf's titles make the list readable. A cold
        // book list costs the titles, not the jobs.
        api.listBooks(signal).catch(() => []),
      ]);
      return {
        jobs: [...jobs].sort((a, b) => (a.created_at < b.created_at ? 1 : -1)),
        titles: new Map(books.map((b) => [b.slug, b.title])),
      };
    },
    [api],
  );

  const jobs = load.data?.jobs ?? null;
  const anyLive = (jobs ?? []).some((j) => isLiveJob(j.status));

  // Only poll while something is actually moving.
  const reload = load.reload;
  useEffect(() => {
    if (!anyLive) return;
    const t = window.setInterval(() => reload(), POLL_MS);
    return () => window.clearInterval(t);
  }, [anyLive, reload]);

  async function resume(id: string) {
    setBusy(id);
    setActionError(null);
    try {
      await api.resumeJob(id);
      navigate(`/jobs/${id}`);
    } catch (e) {
      setActionError(errorMessage(e));
      setBusy(null);
    }
  }

  return (
    <>
      <TopBar crumbs={<span>Jobs</span>}>
        <button className="btn sm" disabled={load.loading} onClick={() => load.reload()}>
          Refresh
        </button>
      </TopBar>

      <div className="page wide">
        <div className="col" style={{ gap: 14 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <h1 className="h1">Jobs</h1>
              <p className="soft" style={{ margin: 0, fontSize: 14 }}>
                Every render, director pass and export, newest first. One runs at a time; the rest
                queue.
              </p>
            </div>
            <span className="mono faint">
              {jobs ? `${jobs.length} job${jobs.length === 1 ? "" : "s"}` : ""}
              {anyLive && <span style={{ color: "var(--signal)" }}> · live</span>}
            </span>
          </div>

          {actionError && <ErrorBox error={actionError} />}
          {load.error && <ErrorBox error={load.error} onRetry={load.reload} />}
          {load.loading && !jobs && <Spinner label="reading the job records" />}

          {jobs && jobs.length === 0 && (
            <div className="panel" style={{ padding: 28, textAlign: "center" }}>
              <p className="soft" style={{ marginBottom: 12 }}>
                No jobs yet. Rendering, directing or exporting a book starts one.
              </p>
              <Link className="btn signal" to="/">
                Go to the shelf
              </Link>
            </div>
          )}

          {jobs && jobs.length > 0 && (
            <div>
              <div className="tablewrap" ref={wrapRef}>
                <table className="sheet cards">
                  <thead>
                    <tr>
                      <th style={{ width: 210 }}>Kind</th>
                      <th>Book</th>
                      <th style={{ width: 120 }}>Status</th>
                      <th style={{ width: 200 }}>Progress</th>
                      <th style={{ width: 130 }}>Started</th>
                      <th style={{ width: 130 }}>Finished</th>
                      <th style={{ width: 170 }}>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {jobs.map((j) => {
                      const live = isLiveJob(j.status);
                      const resumable = isResumableJob(j.status);
                      const title = load.data?.titles.get(j.book);
                      return (
                        <tr key={j.id}>
                          <td className="cardhead" data-label="Kind">
                            <span className={`chip ${live ? "signal" : ""}`}>
                              {KIND_LABEL[j.kind]}
                            </span>
                            {/* A real job id is 28 characters. Left to wrap it
                                made every row three lines tall. */}
                            <div
                              className="mono faint"
                              style={{ fontSize: 10.5, marginTop: 4, whiteSpace: "nowrap" }}
                            >
                              {j.id}
                            </div>
                          </td>
                          <td data-label="Book">
                            <Link to={`/books/${j.book}`}>{title ?? j.book}</Link>
                            {title && (
                              <div className="mono faint" style={{ fontSize: 10.5 }}>
                                {j.book}
                              </div>
                            )}
                          </td>
                          <td className="mono" style={{ fontSize: 12 }} data-label="Status">
                            <span
                              title={STATUS_TITLE[j.status]}
                              style={{
                                color: live
                                  ? "var(--signal)"
                                  : j.status === "failed" || j.status === "interrupted"
                                    ? "var(--risk)"
                                    : undefined,
                              }}
                            >
                              {j.status}
                            </span>
                            {j.error && (
                              <div className="faint" style={{ fontSize: 10.5 }} title={j.error}>
                                {j.error.length > 44 ? `${j.error.slice(0, 44)}…` : j.error}
                              </div>
                            )}
                          </td>
                          <td data-label="Progress">
                            <ProgressBar
                              thin
                              done={j.progress.done}
                              total={j.progress.total}
                              finished={j.status === "done"}
                              label={`${j.kind} ${j.book}: ${j.progress.done} of ${j.progress.total} ${UNIT[j.kind]}`}
                            />
                            <span className="mono faint" style={{ fontSize: 10.5 }}>
                              {j.kind === "export"
                                ? `${j.progress.done}%`
                                : `${j.progress.done}/${j.progress.total} ${UNIT[j.kind]}`}
                              {j.status === "running" && j.progress.eta_s != null
                                ? ` · ETA ~${fmtSeconds(j.progress.eta_s)}`
                                : ""}
                            </span>
                          </td>
                          <td className="mono soft" style={{ fontSize: 12 }} data-label="Started">
                            {j.started_at ? fmtAgo(j.started_at) : <span className="faint">–</span>}
                          </td>
                          <td className="mono soft" style={{ fontSize: 12 }} data-label="Finished">
                            {j.finished_at ? (
                              fmtAgo(j.finished_at)
                            ) : (
                              <span className="faint">{live ? "running" : "–"}</span>
                            )}
                          </td>
                          <td data-label="">
                            <div className="shelf-actions">
                              <Link className="btn sm" to={`/jobs/${j.id}`}>
                                Open
                              </Link>
                              {resumable && (
                                <button
                                  className="btn sm signal"
                                  disabled={busy !== null}
                                  title={STATUS_TITLE[j.status]}
                                  onClick={() => void resume(j.id)}
                                >
                                  {busy === j.id ? "Resuming…" : "Resume"}
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {overflowing && (
                <p className="tablehint">
                  ← the table scrolls sideways for the remaining columns (or narrow the window: it
                  becomes one card per job) →
                </p>
              )}
            </div>
          )}

          <p className="hint">
            A job record is a file, so it survives a restart: anything that was running when the
            service stopped comes back as <span className="mono">interrupted</span> and Resume
            re-queues it. The render cache makes that cheap.
          </p>
        </div>
      </div>
    </>
  );
}
