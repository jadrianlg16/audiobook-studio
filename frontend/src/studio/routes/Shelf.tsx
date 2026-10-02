import { useState } from "react";
import { Link } from "react-router-dom";

import { useApi } from "../api/client";
import { errorMessage } from "../api/http";
import type { BookSummary, EngineOption, Job } from "../api/types";
import { renderAction } from "../domain";
import {
  AudioCounts,
  ConfirmButton,
  EngineChip,
  ErrorBox,
  Spinner,
  useAsync,
  useOverflowHint,
} from "../ui/bits";
import { fmtAgo, fmtDuration } from "../ui/format";
import { SignOffSummary } from "../ui/signoff";
import { TopBar } from "../ui/TopBar";
import { useStartJob } from "../ui/useStartJob";

/**
 * The shelf. One row per book, because the moment direction exists a book
 * stops being a one-shot command and becomes something you return to.
 */
export function Shelf() {
  const api = useApi();
  const job = useStartJob();
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [wrapRef, overflowing] = useOverflowHint<HTMLDivElement>();

  const shelf = useAsync(
    async (signal) => {
      const [books, engines] = await Promise.all([
        api.listBooks(signal),
        // A cold gateway or a settings hiccup costs the engine chips, not the shelf.
        api.listEngines(signal).catch(() => [] as EngineOption[]),
      ]);
      return { books, engines };
    },
    [api],
  );

  const engineById = new Map<string, EngineOption>(
    (shelf.data?.engines ?? []).map((e) => [e.id, e]),
  );

  function act(slug: string, what: "stale" | "direct" | "export") {
    const run = (): Promise<Job> =>
      what === "stale"
        ? api.render(slug, { scope: "stale" })
        : what === "direct"
          ? api.direct(slug, { scope: "all" })
          : api.exportBook(slug, {});
    void job.start(`${slug}:${what}`, run);
  }

  async function remove(slug: string) {
    setBusy(`${slug}:delete`);
    try {
      await api.deleteBook(slug);
      shelf.reload();
    } catch (e) {
      setActionError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <TopBar crumbs={<span>Shelf</span>}>
        <Link className="btn signal" to="/books/new">
          + New book
        </Link>
      </TopBar>

      <div className="page">
        <div className="col" style={{ gap: 14 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <h1 className="h1">Books</h1>
              <p className="soft" style={{ margin: 0, fontSize: 14 }}>
                Direct a book, render what changed, export when every chapter is fresh.
              </p>
            </div>
            <span className="mono faint">
              {shelf.data ? `${shelf.data.books.length} on the shelf` : ""}
            </span>
          </div>

          {(actionError || job.error) && <ErrorBox error={actionError ?? job.error ?? ""} />}
          {shelf.error && <ErrorBox error={shelf.error} onRetry={shelf.reload} />}
          {shelf.loading && <Spinner label="reading the shelf" />}

          {shelf.data && shelf.data.books.length === 0 && (
            <div className="panel" style={{ padding: 28, textAlign: "center" }}>
              <p className="soft" style={{ marginBottom: 12 }}>
                Nothing on the shelf yet.
              </p>
              <Link className="btn signal" to="/books/new">
                + New book
              </Link>
            </div>
          )}

          {shelf.data && shelf.data.books.length > 0 && (
            <div>
              <div className="tablewrap" ref={wrapRef}>
                <table className="sheet cards">
                  <thead>
                    <tr>
                      <th>Book</th>
                      <th>Type</th>
                      <th>Length</th>
                      <th>Engines</th>
                      <th>Directed</th>
                      <th>Audio</th>
                      <th>Sign-off</th>
                      <th>Last rendered</th>
                      <th>Status</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shelf.data.books.map((b) => (
                      <ShelfRow
                        key={b.slug}
                        book={b}
                        engineById={engineById}
                        busy={busy}
                        starting={job.starting}
                        onAct={act}
                        onDelete={remove}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
              {overflowing && (
                <p className="tablehint">
                  ← the table scrolls sideways for the remaining columns (or narrow the window: it
                  becomes one card per book) →
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function ShelfRow({
  book,
  engineById,
  busy,
  starting,
  onAct,
  onDelete,
}: {
  book: BookSummary;
  engineById: Map<string, EngineOption>;
  /** The delete in flight, if any. */
  busy: string | null;
  /** The job start in flight, if any (from useStartJob). */
  starting: string | null;
  onAct: (slug: string, what: "stale" | "direct" | "export") => void;
  onDelete: (slug: string) => void;
}) {
  const fresh = book.exportable;
  const needs = book.needs_render ?? {
    lines: book.audio.stale + book.audio.missing,
    chapters: 0,
  };
  const directable = book.directable?.lines ?? 0;
  const working =
    book.status === "rendering" || book.status === "directing" || book.status === "exporting";
  const isBusy = (what: string) =>
    busy === `${book.slug}:${what}` || starting === `${book.slug}:${what}`;
  /** Any job start anywhere on the shelf disables the rest: one job runs at a time. */
  const anyStarting = starting !== null;

  const renderStale = renderAction(needs);

  return (
    <tr>
      <td className="cardhead" data-label="">
        <div className="shelf-title">
          <Link to={`/books/${book.slug}`}>{book.title}</Link>
        </div>
        <div className="soft" style={{ fontSize: 12.5 }}>
          {book.author}
        </div>
        {book.synthetic && (
          <div style={{ marginTop: 4 }}>
            <span
              className="badge-synth"
              title="The last render ran with STUDIO_OFFLINE=1: these files are the fake synth's test tones, not speech. Re-render with the engines up before you judge anything by ear."
            >
              test audio · fake synth
            </span>
          </div>
        )}
        {book.licence_note && (
          <div className="licence-line" title={book.licence_note}>
            {book.licence_note}
          </div>
        )}
      </td>

      <td className="mono soft" style={{ fontSize: 12 }} data-label="Type">
        {book.profile_label}
      </td>

      <td className="mono" data-label="Length">
        <span style={{ whiteSpace: "nowrap" }}>
          {book.rendered_at && book.duration_ms > 0 ? (
            fmtDuration(book.duration_ms)
          ) : (
            <span className="faint">
              {book.est_minutes < 1 ? "<1 min est" : `~${Math.round(book.est_minutes)} min est`}
            </span>
          )}
        </span>
        <div className="faint" style={{ fontSize: 10.5 }}>
          {book.chapters} ch · {book.utterances} ln
        </div>
      </td>

      <td data-label="Engines">
        <div className="row" style={{ gap: 4 }}>
          {Object.entries(book.engines).map(([id, n]) => (
            <EngineChip
              key={id}
              engine={id}
              count={n}
              option={engineById.get(id)}
              licenceNote={book.licence_note}
            />
          ))}
        </div>
      </td>

      <td className="mono" style={{ whiteSpace: "nowrap" }} data-label="Directed">
        <span style={{ color: book.directed.directed > 0 ? "var(--signal)" : undefined }}>
          {book.directed.directed}
        </span>
        <span className="faint">/{book.directed.total}</span>
        {directable === 0 && (
          <div className="faint" style={{ fontSize: 10 }} title="No engine here takes a note">
            none directable
          </div>
        )}
      </td>

      <td className="mono" style={{ fontSize: 11.5, whiteSpace: "nowrap" }} data-label="Audio">
        <AudioCounts audio={book.audio} />
      </td>

      {/* v1.2: chapters a person has actually listened to and accepted. Absent
          when the studio does not report it — a dash, not a zero. */}
      <td
        className="mono"
        style={{ fontSize: 12, whiteSpace: "nowrap" }}
        data-label="Sign-off"
        title="Chapters approved by ear, out of the chapters included in the audio."
      >
        <SignOffSummary qc={book.qc} />
      </td>

      <td
        className="mono soft"
        style={{ fontSize: 12, whiteSpace: "nowrap" }}
        data-label="Rendered"
      >
        {fmtAgo(book.rendered_at)}
      </td>

      <td className="mono" style={{ fontSize: 12, whiteSpace: "nowrap" }} data-label="Status">
        <span style={{ color: working ? "var(--signal)" : undefined }}>{book.status}</span>
        {book.last_job && (
          <div>
            <Link
              className="mono faint"
              style={{ fontSize: 10.5 }}
              to={`/jobs/${book.last_job.id}`}
            >
              {book.last_job.id} ›
            </Link>
          </div>
        )}
      </td>

      <td data-label="">
        <div className="shelf-actions">
          <Link className="btn sm" to={`/books/${book.slug}`}>
            Open
          </Link>
          <button
            className="btn sm"
            disabled={renderStale.kind === "none" || working || anyStarting}
            title={renderStale.title}
            onClick={() => onAct(book.slug, "stale")}
          >
            {isBusy("stale") ? "Starting…" : renderStale.label}
          </button>
          <button
            className="btn sm signal"
            disabled={working || anyStarting || directable === 0}
            title={
              directable === 0
                ? "No line in this book is on an engine that takes a written note, so a director pass would have nothing to write to. Recast a character first."
                : `Write scene notes for the ${directable} lines that can use them.`
            }
            onClick={() => onAct(book.slug, "direct")}
          >
            {isBusy("direct") ? "Starting…" : "Direct"}
          </button>
          <button
            className="btn sm"
            disabled={!fresh || working || anyStarting}
            title={
              fresh
                ? "Master and export"
                : needs.lines > 0
                  ? `Every chapter must be fresh first: ${needs.lines} line${
                      needs.lines === 1 ? "" : "s"
                    } still need audio.`
                  : `Every chapter must be fresh first: ${needs.chapters} chapter${
                      needs.chapters === 1 ? "" : "s"
                    } need their master audio rebuilt after a single-line render.`
            }
            onClick={() => onAct(book.slug, "export")}
          >
            {isBusy("export") ? "Starting…" : "Export"}
          </button>
          <ConfirmButton
            confirmLabel="Delete for good"
            question={`Delete “${book.title}”?`}
            onConfirm={() => onDelete(book.slug)}
          >
            Delete
          </ConfirmButton>
        </div>
      </td>
    </tr>
  );
}
