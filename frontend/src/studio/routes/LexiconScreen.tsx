import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useApi } from "../api/client";
import { errorMessage } from "../api/http";
import { ErrorBox, Spinner, useAsync } from "../ui/bits";
import { TopBar } from "../ui/TopBar";

interface Row {
  term: string;
  respelling: string;
}

/**
 * The pronunciation lexicon. `GET/PUT /lexicon` worked and had no screen: the
 * single most common real-world audiobook problem — a mispronounced name —
 * required hand-writing a PUT.
 */
export function LexiconScreen() {
  const api = useApi();
  const { slug = "" } = useParams();
  const load = useAsync((signal) => api.getLexicon(slug, signal), [api, slug]);

  const [rows, setRows] = useState<Row[]>([]);
  const [term, setTerm] = useState("");
  const [respelling, setRespelling] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!load.data) return;
    setRows(Object.entries(load.data.entries).map(([t, r]) => ({ term: t, respelling: r })));
  }, [load.data]);

  const occurrences = load.data?.occurrences ?? {};
  const dirty =
    !!load.data &&
    (rows.length !== Object.keys(load.data.entries).length ||
      rows.some((r) => load.data!.entries[r.term] !== r.respelling));

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const entries: Record<string, string> = {};
      for (const r of rows) {
        if (r.term.trim()) entries[r.term.trim()] = r.respelling.trim();
      }
      await api.putLexicon(slug, entries);
      setSaved(true);
      load.reload();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  function add() {
    if (!term.trim()) return;
    setRows((prev) => [
      ...prev.filter((r) => r.term !== term.trim()),
      { term: term.trim(), respelling: respelling.trim() },
    ]);
    setTerm("");
    setRespelling("");
    setSaved(false);
  }

  return (
    <>
      <TopBar
        crumbs={
          <>
            <Link to={`/books/${slug}`}>{slug}</Link>
            <span className="faint">/</span>
            <span>Lexicon</span>
          </>
        }
      >
        <Link className="btn sm" to={`/books/${slug}`}>
          ← Back to the desk
        </Link>
        <button className="btn signal" disabled={saving || !dirty} onClick={() => void save()}>
          {saving ? "Saving…" : saved && !dirty ? "Saved" : "Save lexicon"}
        </button>
      </TopBar>

      <div className="page">
        <div className="col" style={{ gap: 14 }}>
          <div>
            <h1 className="h1">Pronunciation</h1>
            <p className="soft" style={{ margin: 0, fontSize: 14 }}>
              A respelling is applied to every line that says the term, word-boundary anchored and
              case-preserving. Saving makes exactly those lines stale — the ones that say it, not
              the ones the character speaks.
            </p>
          </div>

          {error && <ErrorBox error={error} />}
          {load.loading && <Spinner label="reading the lexicon" />}
          {load.error && <ErrorBox error={load.error} onRetry={load.reload} />}

          {load.data && (
            <>
              <div className="tablewrap">
                <table className="sheet" style={{ minWidth: 560 }}>
                  <thead>
                    <tr>
                      <th style={{ width: 200 }}>Term</th>
                      <th>Respelling</th>
                      <th style={{ width: 120 }}>Lines</th>
                      <th style={{ width: 90 }} />
                    </tr>
                  </thead>
                  <tbody>
                    {rows.length === 0 && (
                      <tr>
                        <td colSpan={4} className="hint">
                          Nothing in this book's lexicon yet.
                        </td>
                      </tr>
                    )}
                    {rows.map((r, i) => {
                      const n = occurrences[r.term];
                      return (
                        <tr key={`${r.term}-${i}`}>
                          <td className="mono">{r.term}</td>
                          <td>
                            <input
                              type="text"
                              aria-label={`Respelling for ${r.term}`}
                              value={r.respelling}
                              placeholder="eh-LAR-uh"
                              onChange={(e) =>
                                setRows((prev) =>
                                  prev.map((x, j) =>
                                    j === i ? { ...x, respelling: e.target.value } : x,
                                  ),
                                )
                              }
                            />
                          </td>
                          <td className="mono">
                            {n === undefined ? (
                              <span className="faint">new</span>
                            ) : n === 0 ? (
                              <span
                                className="faint"
                                title="This term appears in no line: it does nothing"
                              >
                                0 — does nothing
                              </span>
                            ) : (
                              `${n} line${n === 1 ? "" : "s"}`
                            )}
                          </td>
                          <td>
                            <button
                              className="btn sm danger"
                              onClick={() => setRows((prev) => prev.filter((_, j) => j !== i))}
                            >
                              Remove
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              <div className="row" style={{ alignItems: "flex-end", gap: 8 }}>
                <div className="field" style={{ maxWidth: 220 }}>
                  <label htmlFor="lex-term">Term</label>
                  <input
                    id="lex-term"
                    type="text"
                    value={term}
                    placeholder="Elara"
                    onChange={(e) => setTerm(e.target.value)}
                  />
                </div>
                <div className="field" style={{ maxWidth: 260 }}>
                  <label htmlFor="lex-resp">Respelling</label>
                  <input
                    id="lex-resp"
                    type="text"
                    value={respelling}
                    placeholder="eh-LAR-uh"
                    onChange={(e) => setRespelling(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") add();
                    }}
                  />
                </div>
                <button className="btn" disabled={!term.trim()} onClick={add}>
                  + Add
                </button>
                {dirty && <span className="hint">Unsaved. Nothing changes until you save.</span>}
              </div>

              {Object.keys(load.data.global).length > 0 && (
                <div className="col" style={{ gap: 6 }}>
                  <span className="eyebrow">Global lexicon</span>
                  <span className="hint">
                    Applied to every book. Edited in output/lexicon.json, not here.
                  </span>
                  <div className="row" style={{ gap: 6 }}>
                    {Object.entries(load.data.global).map(([t, r]) => (
                      <span className="chip" key={t}>
                        {t} → {r}
                        {occurrences[t] !== undefined && (
                          <span className="faint">{occurrences[t]}</span>
                        )}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}
