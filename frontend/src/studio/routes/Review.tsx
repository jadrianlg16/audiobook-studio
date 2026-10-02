import { useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useApi } from "../api/client";
import { errorMessage } from "../api/http";
import type { Pass, PassScene } from "../api/types";
import { ErrorBox, Spinner, useAsync } from "../ui/bits";
import { sideParts, wordDiff } from "../ui/diff";
import { fmtSeconds } from "../ui/format";
import { TopBar } from "../ui/TopBar";

/**
 * Direction review. Rejections get the space, because they are the only
 * output that means something is wrong: the model returned text that was not
 * the book's, and the validator threw it away.
 */
export function Review() {
  const api = useApi();
  const navigate = useNavigate();
  const { slug = "", passId = "" } = useParams();

  const load = useAsync((signal) => api.getPass(slug, passId, signal), [api, slug, passId]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [rowBusy, setRowBusy] = useState<string[]>([]);
  const [onlyRejected, setOnlyRejected] = useState(false);
  const decideSeq = useRef(0);

  const pass = load.data;

  /** What "Accept N & close pass" would accept. */
  const pending = useMemo(
    () =>
      (pass?.scenes ?? []).filter(
        (s) => s.decision === "pending" && s.status !== "rejected" && s.status !== "ignored",
      ),
    [pass],
  );
  /** Everything still undecided that *can* be decided (ignored scenes cannot). */
  const undecided = useMemo(
    () => (pass?.scenes ?? []).filter((s) => s.decision === "pending" && s.status !== "ignored"),
    [pass],
  );
  const visible = useMemo(
    () => (pass?.scenes ?? []).filter((s) => !onlyRejected || s.status === "rejected"),
    [pass, onlyRejected],
  );

  async function decide(
    sceneIds: string[],
    decision: Exclude<PassScene["decision"], "pending">,
    instruction?: string,
  ) {
    if (sceneIds.length === 0) return;
    setError(null);
    setRowBusy((prev) => [...prev, ...sceneIds]);
    const mine = ++decideSeq.current;
    try {
      const next = await api.decide(slug, passId, {
        decisions: sceneIds.map((scene_id) => ({
          scene_id,
          decision,
          ...(instruction ? { instruction } : {}),
        })),
      });
      // A slower earlier response would otherwise replace the whole pass and
      // visibly revert a decision made since.
      if (mine === decideSeq.current) load.set(next);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setRowBusy((prev) => prev.filter((id) => !sceneIds.includes(id)));
    }
  }

  async function acceptAndClose() {
    if (!pass) return;
    setBusy(true);
    setError(null);
    try {
      if (pending.length > 0) {
        const next = await api.decide(slug, passId, {
          decisions: pending.map((s) => ({ scene_id: s.scene_id, decision: "accepted" as const })),
        });
        load.set(next);
      }
      await api.applyPass(slug, passId);
      navigate(`/books/${slug}`);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  async function retry(sceneId: string) {
    setBusy(true);
    setError(null);
    try {
      const job = await api.retryPass(slug, passId, [sceneId]);
      navigate(`/jobs/${job.id}`);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  if (load.loading) {
    return (
      <>
        <TopBar crumbs={<span className="mono">{passId}</span>} />
        <div className="page">
          <Spinner label="reading the pass" />
        </div>
      </>
    );
  }
  if (load.error || !pass) {
    return (
      <>
        <TopBar crumbs={<span className="mono">{passId}</span>} />
        <div className="page">
          <ErrorBox error={load.error ?? "Pass not found"} onRetry={load.reload} />
        </div>
      </>
    );
  }

  const applied = !!pass.applied_at;

  return (
    <>
      <TopBar
        crumbs={
          <>
            <Link to={`/books/${slug}`}>{slug}</Link>
            <span className="faint">/</span>
            <span>Director pass</span>
            <span className="faint">/</span>
            <span>{pass.model}</span>
            <span className="faint">/</span>
            <span>{pass.counts.scenes} scenes</span>
            <span className="faint">/</span>
            <span>{fmtSeconds(pass.elapsed_s)}</span>
          </>
        }
      >
        {applied ? (
          <span className="chip">applied</span>
        ) : (
          <>
            <button
              className="btn sm danger"
              disabled={busy || undecided.length === 0}
              title="Mark every scene still awaiting a decision as rejected."
              onClick={() =>
                void decide(
                  undecided.map((s) => s.scene_id),
                  "rejected",
                )
              }
            >
              Reject all {undecided.length} pending
            </button>
            <button className="btn signal" disabled={busy} onClick={() => void acceptAndClose()}>
              Accept {pending.length} &amp; close pass
            </button>
          </>
        )}
      </TopBar>

      <div className="page wide">
        <div className="col" style={{ gap: 14 }}>
          {error && <ErrorBox error={error} />}

          <div className="tiles">
            <div className="tile kept">
              <span className="eyebrow">Kept</span>
              <span className="n">{pass.counts.kept}</span>
            </div>
            <div className="tile signal">
              <span className="eyebrow" style={{ color: "var(--signal)" }}>
                Rejected
              </span>
              <span className="n">{pass.counts.rejected}</span>
              <span className="mono faint" style={{ fontSize: 10.5 }}>
                model edited the text
              </span>
            </div>
            <div className="tile">
              <span className="eyebrow">Ignored</span>
              <span className="n">{pass.counts.ignored}</span>
              <span className="mono faint" style={{ fontSize: 10.5 }}>
                nothing usable — nothing to decide
              </span>
            </div>
            <div className="tile">
              <span className="eyebrow">Target engine</span>
              <span className="n" style={{ fontSize: 18, paddingTop: 6 }}>
                {pass.target_engine}
              </span>
              <span className="mono faint" style={{ fontSize: 10.5 }}>
                {pass.provider} · {pass.prompt_version}
              </span>
            </div>
          </div>

          <div className="row">
            <button
              className={`btn sm ${onlyRejected ? "on" : ""}`}
              aria-pressed={onlyRejected}
              onClick={() => setOnlyRejected((v) => !v)}
            >
              show only rejected ({pass.counts.rejected})
            </button>
            <span className="hint">
              Showing {visible.length} of {pass.scenes.length} scenes.
            </span>
          </div>

          <div className="tablewrap">
            <table className="sheet">
              <thead>
                <tr>
                  <th style={{ width: 70 }}>Scene</th>
                  <th>Direction</th>
                  <th style={{ width: 120 }}>Tags</th>
                  <th style={{ width: 92 }}>Status</th>
                  <th style={{ width: 210 }}>Decision</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((scene) => (
                  <SceneRow
                    key={scene.scene_id}
                    scene={scene}
                    pass={pass}
                    editing={editing === scene.scene_id}
                    draft={draft}
                    setDraft={setDraft}
                    onEdit={() => {
                      setEditing(scene.scene_id);
                      setDraft(scene.final_instruction ?? scene.instruction ?? "");
                    }}
                    onCancelEdit={() => setEditing(null)}
                    onSaveEdit={() => {
                      setEditing(null);
                      void decide([scene.scene_id], "edited", draft.trim());
                    }}
                    onDecide={(d) => void decide([scene.scene_id], d)}
                    onRetry={() => void retry(scene.scene_id)}
                    busy={busy || rowBusy.includes(scene.scene_id)}
                  />
                ))}
              </tbody>
            </table>
          </div>

          <p className="hint">
            Decisions are saved as you make them. Applying writes the accepted notes into the script
            and flips those lines to stale. A scene's pace and emotion are written to every line,
            whatever engine it is on; the written note only reaches engines that take one.
          </p>
        </div>
      </div>
    </>
  );
}

function DecisionMark({ decision }: { decision: PassScene["decision"] }) {
  if (decision === "pending") return <span className="mono faint">pending</span>;
  const colour =
    decision === "rejected"
      ? "var(--risk)"
      : decision === "accepted"
        ? "var(--ship)"
        : "var(--signal)";
  return (
    <span className="mono" style={{ color: colour, fontSize: 11 }}>
      {decision}
    </span>
  );
}

function SceneRow({
  scene,
  pass,
  editing,
  draft,
  setDraft,
  onEdit,
  onCancelEdit,
  onSaveEdit,
  onDecide,
  onRetry,
  busy,
}: {
  scene: PassScene;
  pass: Pass;
  editing: boolean;
  draft: string;
  setDraft: (v: string) => void;
  onEdit: () => void;
  onCancelEdit: () => void;
  onSaveEdit: () => void;
  onDecide: (d: Exclude<PassScene["decision"], "pending">) => void;
  onRetry: () => void;
  busy: boolean;
}) {
  const rejected = scene.status === "rejected";
  const ignored = scene.status === "ignored";
  const label = `${scene.chapter_index + 1}.${scene.scene_index + 1}`;
  const applied = !!pass.applied_at;
  const parts = rejected && scene.model_text ? wordDiff(scene.source_text, scene.model_text) : [];

  return (
    <tr className={rejected ? "rejected-row" : undefined}>
      <td className="mono" style={{ whiteSpace: "nowrap" }}>
        {label}
        <div className="faint" style={{ fontSize: 10 }}>
          {scene.scene_id}
        </div>
      </td>

      <td>
        {editing ? (
          <textarea
            className="note"
            rows={2}
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                onSaveEdit();
              }
              if (e.key === "Escape") onCancelEdit();
            }}
          />
        ) : rejected ? (
          <div className="col" style={{ gap: 8 }}>
            <span className="mono" style={{ color: "var(--signal)", fontSize: 10.5 }}>
              REJECTED — annotation was not the source passage
            </span>
            <div style={{ display: "grid", gridTemplateColumns: "48px minmax(0,1fr)", gap: 8 }}>
              <span className="mono faint" style={{ fontSize: 10 }}>
                book
              </span>
              <p className="difftext" style={{ margin: 0 }}>
                {sideParts(parts, "before").map((p, i) => (
                  <span key={i} className={p.op === "del" ? "diff-del" : undefined}>
                    {p.text}
                  </span>
                ))}
              </p>
              <span className="mono" style={{ fontSize: 10, color: "var(--signal)" }}>
                model
              </span>
              <p className="difftext" style={{ margin: 0 }}>
                {sideParts(parts, "after").map((p, i) => (
                  <span key={i} className={p.op === "ins" ? "diff-ins" : undefined}>
                    {p.text}
                  </span>
                ))}
              </p>
            </div>
            {scene.instruction && (
              <p className="dir" style={{ margin: 0 }}>
                ({scene.final_instruction ?? scene.instruction})
              </p>
            )}
          </div>
        ) : scene.instruction ? (
          <p className="dir" style={{ margin: 0 }}>
            ({scene.final_instruction ?? scene.instruction})
          </p>
        ) : (
          <span className="mono faint" style={{ fontSize: 11 }}>
            nothing usable for {pass.target_engine}
          </span>
        )}

        {/* Context for the human, never sent to an engine. */}
        {scene.scene_note && <p className="scenenote">scene: {scene.scene_note}</p>}

        <div className="faint" style={{ fontSize: 11, marginTop: 4 }}>
          {scene.summary}
          {scene.pace != null && (
            <span
              className="mono"
              style={{ marginLeft: 8, color: "var(--signal)" }}
              title="Relative reading speed the director asked for. Applied to every line, including engines that take no written note."
            >
              pace {scene.pace.toFixed(2)}×
            </span>
          )}
        </div>
      </td>

      <td className="mono" style={{ fontSize: 10.5 }}>
        {Object.keys(scene.tags_used).length === 0
          ? "—"
          : Object.entries(scene.tags_used).map(([t, n]) => (
              <div key={t}>
                {t} ×{n}
              </div>
            ))}
      </td>

      <td className="mono" style={{ fontSize: 10.5, whiteSpace: "nowrap" }}>
        <span
          style={{
            color:
              scene.status === "kept"
                ? "var(--ship)"
                : scene.status === "rejected"
                  ? "var(--signal)"
                  : "var(--ink-faint)",
          }}
        >
          <span className={`dot ${scene.status === "kept" ? "cached" : "stale"}`} aria-hidden />{" "}
          {scene.status === "rejected" ? "review" : scene.status}
        </span>
        <div style={{ marginTop: 4 }}>
          <DecisionMark decision={scene.decision} />
        </div>
        {scene.attempts > 1 && <div className="faint">attempt {scene.attempts}</div>}
      </td>

      <td>
        <div className="shelf-actions">
          {editing ? (
            <>
              <button className="btn sm signal" disabled={busy} onClick={onSaveEdit}>
                Save note
              </button>
              <button className="btn sm ghost" onClick={onCancelEdit}>
                Cancel
              </button>
            </>
          ) : ignored ? (
            // The model returned nothing usable for this engine. Accepting or
            // rejecting an empty scene is a click with no meaning.
            <span className="hint" style={{ textAlign: "right" }}>
              nothing to decide
              <br />
              <button className="btn sm" disabled={busy || applied} onClick={onRetry}>
                Retry this scene
              </button>
            </span>
          ) : (
            <>
              {rejected && (
                <>
                  <button
                    className="btn sm"
                    disabled={applied || busy}
                    title="Accept the note, keep the book's text (no tags)"
                    onClick={() => onDecide("kept_text")}
                  >
                    Keep book text
                  </button>
                  <button className="btn sm" disabled={busy} onClick={onRetry}>
                    Retry
                  </button>
                </>
              )}
              <button
                className="btn sm"
                disabled={applied || busy || !scene.instruction}
                onClick={() => onDecide("accepted")}
              >
                Accept
              </button>
              <button className="btn sm" disabled={applied || busy} onClick={onEdit}>
                Edit
              </button>
              <button
                className="btn sm danger"
                disabled={applied || busy}
                onClick={() => onDecide("rejected")}
              >
                Reject
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  );
}
