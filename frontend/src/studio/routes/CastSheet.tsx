import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useApi } from "../api/client";
import { errorMessage } from "../api/http";
import type { CastEntry, EngineOption } from "../api/types";
import { ErrorBox, Spinner, useAsync } from "../ui/bits";
import { TopBar } from "../ui/TopBar";

/**
 * The ingest writes a one-letter gender code into `notes` for a character it
 * guessed at. "f" on its own is not a note, it is a code nobody outside the
 * pipeline can read, so the sheet says what it means and leaves the value alone.
 */
const GENDER_CODE: Record<string, string> = { f: "female", m: "male", n: "unknown" };

function decodeNotes(notes: string): string | null {
  return GENDER_CODE[notes.trim().toLowerCase()] ?? null;
}

/**
 * The cast sheet. `PATCH /cast` and `POST /cast/merge` existed and were tested;
 * nothing in the UI called them, so a mis-cast character or a name the
 * attributor split in three could only be fixed by hand-writing HTTP.
 */
export function CastSheet() {
  const api = useApi();
  const { slug = "" } = useParams();

  const load = useAsync(
    async (signal) => {
      const [cast, engines] = await Promise.all([
        api.getCast(slug, signal),
        api.listEngines(signal).catch(() => [] as EngineOption[]),
      ]);
      return { cast, engines };
    },
    [api, slug],
  );

  const [drafts, setDrafts] = useState<Record<string, CastEntry>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mergeFrom, setMergeFrom] = useState<string | null>(null);
  const [mergeInto, setMergeInto] = useState("");

  const summary = load.data?.cast;
  const engines = load.data?.engines ?? [];

  const sharedByCharacter = useMemo(() => {
    const m = new Map<string, string[]>();
    for (const group of summary?.shared_voices ?? []) {
      for (const c of group)
        m.set(
          c,
          group.filter((x) => x !== c),
        );
    }
    return m;
  }, [summary]);

  function draftFor(entry: CastEntry): CastEntry {
    return drafts[entry.character] ?? entry;
  }

  function edit(entry: CastEntry, patch: Partial<CastEntry>) {
    setDrafts((prev) => ({
      ...prev,
      [entry.character]: { ...draftFor(entry), ...patch, character: entry.character },
    }));
  }

  async function save(entry: CastEntry) {
    const next = drafts[entry.character];
    if (!next) return;
    setBusy(entry.character);
    setError(null);
    try {
      await api.patchCast(slug, [next]);
      setDrafts((prev) => {
        const copy = { ...prev };
        delete copy[entry.character];
        return copy;
      });
      load.reload();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  async function merge(from: string, into: string) {
    if (!into.trim() || into.trim() === from) return;
    setBusy(from);
    setError(null);
    try {
      await api.mergeCast(slug, from, into.trim());
      setMergeFrom(null);
      setMergeInto("");
      load.reload();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <TopBar
        crumbs={
          <>
            <Link to={`/books/${slug}`}>{slug}</Link>
            <span className="faint">/</span>
            <span>Cast sheet</span>
          </>
        }
      >
        <Link className="btn sm" to={`/books/${slug}`}>
          ← Back to the desk
        </Link>
      </TopBar>

      <div className="page wide">
        <div className="col" style={{ gap: 14 }}>
          <div>
            <h1 className="h1">Cast</h1>
            <p className="soft" style={{ margin: 0, fontSize: 14 }}>
              One row per character. Changing a voice or an engine re-stamps that character's lines
              and makes exactly those stale — nothing else in the book moves.
            </p>
          </div>

          {error && <ErrorBox error={error} />}
          {load.loading && <Spinner label="reading the cast" />}
          {load.error && <ErrorBox error={load.error} onRetry={load.reload} />}

          {summary && summary.shared_voices.length > 0 && (
            <div className="notice risk">
              Two characters share a voice:{" "}
              {summary.shared_voices.map((g) => g.join(" + ")).join("; ")}. The listener cannot tell
              them apart — give one of them another voice.
            </div>
          )}

          {summary && (
            <div className="tablewrap">
              <table className="sheet">
                <thead>
                  <tr>
                    <th style={{ width: 150 }}>Character</th>
                    <th style={{ width: 70 }}>Lines</th>
                    <th>First line</th>
                    <th style={{ width: 130 }}>Engine</th>
                    <th style={{ width: 170 }}>Voice</th>
                    <th style={{ width: 200 }}>Notes</th>
                    <th style={{ width: 210 }}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.cast.map((entry) => {
                    const d = draftFor(entry);
                    const dirty = !!drafts[entry.character];
                    const engine = engines.find((e) => e.id === d.engine);
                    const clash = sharedByCharacter.get(entry.character) ?? [];
                    return (
                      <tr key={entry.character}>
                        <td className="mono">
                          {entry.character}
                          {clash.length > 0 && (
                            <div className="faint" style={{ fontSize: 10, color: "var(--risk)" }}>
                              same voice as {clash.join(", ")}
                            </div>
                          )}
                        </td>
                        <td className="mono">{summary.lines[entry.character] ?? 0}</td>
                        <td style={{ fontSize: 13 }}>
                          <span className="soft">{summary.first_line[entry.character] || "—"}</span>
                        </td>
                        <td>
                          <select
                            aria-label={`Engine for ${entry.character}`}
                            value={d.engine}
                            onChange={(e) => edit(entry, { engine: e.target.value })}
                          >
                            {!engines.some((e) => e.id === d.engine) && (
                              <option value={d.engine}>{d.engine} (not in settings)</option>
                            )}
                            {engines.map((e) => (
                              <option key={e.id} value={e.id}>
                                {e.id}
                                {e.instruction ? " · takes direction" : ""}
                                {e.available ? "" : " (not running)"}
                              </option>
                            ))}
                          </select>
                        </td>
                        <td>
                          {engine && engine.voices.length > 0 ? (
                            <select
                              aria-label={`Voice for ${entry.character}`}
                              value={d.voice_id ?? ""}
                              onChange={(e) => edit(entry, { voice_id: e.target.value || null })}
                            >
                              <option value="">— none —</option>
                              {d.voice_id && !engine.voices.some((v) => v.id === d.voice_id) && (
                                <option value={d.voice_id}>
                                  {d.voice_id} (not on {engine.id})
                                </option>
                              )}
                              {engine.voices.map((v) => (
                                <option key={v.id} value={v.id}>
                                  {v.id} — {v.label}
                                </option>
                              ))}
                            </select>
                          ) : (
                            <textarea
                              aria-label={`Voice description for ${entry.character}`}
                              rows={2}
                              placeholder="A woman in her late thirties, low and dry."
                              value={d.voice_design ?? ""}
                              onChange={(e) =>
                                edit(entry, { voice_design: e.target.value || null })
                              }
                            />
                          )}
                        </td>
                        <td>
                          <input
                            type="text"
                            aria-label={`Notes for ${entry.character}`}
                            value={d.notes}
                            onChange={(e) => edit(entry, { notes: e.target.value })}
                          />
                          {decodeNotes(d.notes) && (
                            <span
                              className="hint"
                              title={`The ingest wrote the code “${d.notes.trim()}”. Free text is kept as written.`}
                            >
                              {decodeNotes(d.notes)}
                            </span>
                          )}
                        </td>
                        <td>
                          <div className="shelf-actions">
                            <button
                              className="btn sm signal"
                              disabled={!dirty || busy !== null}
                              onClick={() => void save(entry)}
                            >
                              {busy === entry.character ? "Saving…" : "Save"}
                            </button>
                            {dirty && (
                              <button
                                className="btn sm ghost"
                                onClick={() =>
                                  setDrafts((prev) => {
                                    const copy = { ...prev };
                                    delete copy[entry.character];
                                    return copy;
                                  })
                                }
                              >
                                Revert
                              </button>
                            )}
                            <button
                              className="btn sm"
                              disabled={busy !== null}
                              onClick={() => {
                                setMergeFrom(
                                  mergeFrom === entry.character ? null : entry.character,
                                );
                                setMergeInto("");
                              }}
                            >
                              Rename / merge
                            </button>
                          </div>
                          {mergeFrom === entry.character && (
                            <div className="col" style={{ gap: 4, marginTop: 6 }}>
                              <label className="hint" htmlFor={`merge-${entry.character}`}>
                                Move every line of {entry.character} into:
                              </label>
                              <input
                                id={`merge-${entry.character}`}
                                type="text"
                                list="cast-names"
                                autoFocus
                                placeholder="an existing name, or a new one"
                                value={mergeInto}
                                onChange={(e) => setMergeInto(e.target.value)}
                                onKeyDown={(e) => {
                                  if (e.key === "Enter") void merge(entry.character, mergeInto);
                                  if (e.key === "Escape") setMergeFrom(null);
                                }}
                              />
                              <div className="row" style={{ gap: 4 }}>
                                <button
                                  className="btn sm signal"
                                  disabled={!mergeInto.trim() || busy !== null}
                                  onClick={() => void merge(entry.character, mergeInto)}
                                >
                                  Merge
                                </button>
                                <button className="btn sm ghost" onClick={() => setMergeFrom(null)}>
                                  Cancel
                                </button>
                              </div>
                              <span className="hint">
                                A new name renames the character; an existing one merges the two.
                                Their lines take the target's voice and become stale.
                              </span>
                            </div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <datalist id="cast-names">
                {summary.cast.map((c) => (
                  <option key={c.character} value={c.character} />
                ))}
              </datalist>
            </div>
          )}

          <p className="hint">
            An engine with no named voices (Breeze) reads the voice description instead; it is
            prefixed to every one of that character's lines.
          </p>
        </div>
      </div>
    </>
  );
}
