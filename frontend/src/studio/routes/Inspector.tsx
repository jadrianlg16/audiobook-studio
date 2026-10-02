import { useEffect, useRef, useState } from "react";

import type { CastEntry, EngineOption, Line, LinePatch } from "../api/types";
import { isGuess } from "../domain";
import { StateLabel } from "../ui/bits";
import { DIRECTION_PLACEHOLDER } from "../ui/copy";
import { fmtDuration, shortKey } from "../ui/format";

const NEW_CHARACTER = "__new__";

/**
 * The desk's right column. Everything that decides what this one line will sound
 * like, who it belongs to, and the cache state that says whether it still
 * matches the audio.
 */
export function Inspector({
  line,
  cast,
  engines,
  onPatch,
  onFocusLine,
  onRerender,
  onPlay,
  rendering,
  auditioning,
  playError,
}: {
  line: Line | null;
  cast: CastEntry[];
  engines: EngineOption[];
  onPatch: (patch: LinePatch) => Promise<void>;
  /** Put focus back on this line's row in the script (after Esc in the note). */
  onFocusLine: () => void;
  onRerender: () => void;
  onPlay: () => void;
  rendering: boolean;
  auditioning: boolean;
  playError: string | null;
}) {
  const [note, setNote] = useState("");
  const [spoken, setSpoken] = useState("");
  const [qcNote, setQcNote] = useState("");
  const [newCharacter, setNewCharacter] = useState<string | null>(null);
  const noteRef = useRef<HTMLTextAreaElement>(null);
  const spokenRef = useRef<HTMLTextAreaElement>(null);
  const qcRef = useRef<HTMLTextAreaElement>(null);
  const lineIdRef = useRef<string | null>(null);
  /**
   * Set by a key that finishes the note itself, so the blur that key causes
   * does not save as well: that blur runs the onBlur of the render the key was
   * pressed in, which still holds the note as typed.
   */
  const noteSettled = useRef(false);

  /**
   * Reset the drafts from the server, except into a field that currently has
   * focus: a PATCH for one field resolving while the user types in another used
   * to overwrite what they were writing.
   */
  useEffect(() => {
    const changedLine = lineIdRef.current !== (line?.id ?? null);
    lineIdRef.current = line?.id ?? null;
    if (changedLine) setNewCharacter(null);
    const active = typeof document !== "undefined" ? document.activeElement : null;
    if (changedLine || active !== noteRef.current) setNote(line?.render.instruction ?? "");
    if (changedLine || active !== spokenRef.current)
      setSpoken(line?.render.spoken_text ?? line?.text ?? "");
    if (changedLine || active !== qcRef.current) setQcNote(line?.note ?? "");
  }, [line?.id, line?.render.instruction, line?.render.spoken_text, line?.text, line?.note]);

  if (!line) {
    return (
      <aside className="inspector">
        <span className="eyebrow">Line inspector</span>
        <p className="hint">Select a line in the script.</p>
      </aside>
    );
  }

  const engine = engines.find((e) => e.id === line.render.engine);
  const castEntry = cast.find((c) => c.character === line.speaker);
  const takesDirection = engine?.instruction ?? false;
  const tags = engine?.audio_tags ?? [];
  const previousKey = line.audio?.cache_key ?? null;
  const knownEngine = engine !== undefined;
  const voiceKnown =
    line.render.voice_id === null ||
    (engine?.voices ?? []).some((v) => v.id === line.render.voice_id);
  const characters = [...new Set([...cast.map((c) => c.character), line.speaker])].sort();

  function insertTag(tag: string) {
    const el = spokenRef.current;
    const at = el?.selectionStart ?? spoken.length;
    const next =
      `${spoken.slice(0, at)}${spoken.slice(0, at).endsWith(" ") ? "" : " "}${tag} ${spoken.slice(at)}`.replace(
        /\s+/g,
        " ",
      );
    setSpoken(next.trim());
    void onPatch({ spoken_text: next.trim() });
  }

  return (
    <aside className="inspector">
      <span className="eyebrow">Line inspector</span>

      {/* ------------------------------------------------------- speaker */}
      <div className="field">
        <label htmlFor="ins-speaker">Speaker</label>
        <select
          id="ins-speaker"
          value={newCharacter !== null ? NEW_CHARACTER : line.speaker}
          onChange={(e) => {
            if (e.target.value === NEW_CHARACTER) {
              setNewCharacter("");
              return;
            }
            setNewCharacter(null);
            void onPatch({ speaker: e.target.value });
          }}
        >
          {characters.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
          <option value={NEW_CHARACTER}>New character…</option>
        </select>
        {newCharacter !== null && (
          <div className="row" style={{ gap: 4 }}>
            <input
              type="text"
              autoFocus
              value={newCharacter}
              placeholder="Mara"
              onChange={(e) => setNewCharacter(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && newCharacter.trim()) {
                  void onPatch({ speaker: newCharacter.trim() });
                  setNewCharacter(null);
                }
                if (e.key === "Escape") setNewCharacter(null);
              }}
            />
            <button
              className="btn sm signal"
              disabled={!newCharacter.trim()}
              onClick={() => {
                void onPatch({ speaker: newCharacter.trim() });
                setNewCharacter(null);
              }}
            >
              Add
            </button>
            <button className="btn sm ghost" onClick={() => setNewCharacter(null)}>
              Cancel
            </button>
          </div>
        )}
        <span className="hint">
          {line.attribution ? (
            <>
              attribution: <span className="mono">{line.attribution.basis}</span> ·{" "}
              <span className="mono">{line.attribution.confidence.toFixed(2)}</span>
              {isGuess(line.attribution) && " — the attributor was guessing; check this one."}
            </>
          ) : (
            "Reassigning a line takes the character's voice and engine, and makes the line stale."
          )}
        </span>
      </div>

      {/* -------------------------------------------------------- engine */}
      <div className="field">
        <label htmlFor="ins-engine">Engine</label>
        <select
          id="ins-engine"
          value={line.render.engine}
          onChange={(e) => void onPatch({ engine: e.target.value })}
        >
          {/* An engine the settings do not list would otherwise display as
              whatever sits first in the list. */}
          {!knownEngine && (
            <option value={line.render.engine}>{line.render.engine} (not in settings)</option>
          )}
          {engines.map((e) => (
            <option key={e.id} value={e.id}>
              {e.id}
              {e.instruction ? " · takes direction" : ""}
              {e.non_commercial ? " · non-commercial" : ""}
              {e.available ? "" : " (not running)"}
            </option>
          ))}
        </select>
        {engine && !engine.available && (
          <span className="hint">
            This engine's container is not answering right now. You can still route lines to it; the
            render will fail until it is up.
          </span>
        )}
        {engine?.non_commercial && (
          <span className="hint" style={{ color: "var(--risk)" }}>
            {engine.license}. Covers self-hosted output, so anything rendered on it is personal use
            only.
          </span>
        )}
        {knownEngine && !takesDirection && (
          <span className="hint">
            {line.render.engine} ignores a written note. Route this line to an engine that takes
            direction for the note to do anything.
          </span>
        )}
      </div>

      {/* --------------------------------------------------------- voice */}
      <div className="field">
        <label htmlFor="ins-voice">Voice</label>
        {engine && engine.voices.length > 0 ? (
          <select
            id="ins-voice"
            value={line.render.voice_id ?? ""}
            onChange={(e) => void onPatch({ voice_id: e.target.value || null })}
          >
            <option value="">— none —</option>
            {/* A voice retained across an engine change matches no option; show
                it rather than quietly displaying "— none —". */}
            {!voiceKnown && line.render.voice_id && (
              <option value={line.render.voice_id}>
                {line.render.voice_id} (not on {engine.id})
              </option>
            )}
            {engine.voices.map((v) => (
              <option key={v.id} value={v.id}>
                {line.speaker} — {v.id}
              </option>
            ))}
          </select>
        ) : (
          <>
            <p className="mono" style={{ fontSize: 11.5, margin: 0 }}>
              {line.speaker}
            </p>
            <p className="hint" style={{ fontStyle: "italic" }}>
              {line.render.voice_design ??
                castEntry?.voice_design ??
                "No named voices on this engine; the cast sheet's voice description is used."}
            </p>
          </>
        )}
      </div>

      {/* ----------------------------------------------------- direction */}
      <div className="field signal">
        <label htmlFor="ins-note">Direction</label>
        <textarea
          id="ins-note"
          ref={noteRef}
          className="note"
          rows={4}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          onBlur={() => {
            if (noteSettled.current) {
              noteSettled.current = false;
              return;
            }
            if (note !== (line.render.instruction ?? ""))
              void onPatch({ instruction: note || null });
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              // Save here, once; the blur below must not save again.
              e.preventDefault();
              noteSettled.current = true;
              if (note !== (line.render.instruction ?? ""))
                void onPatch({ instruction: note || null });
              e.currentTarget.blur();
            }
            if (e.key === "Escape") {
              // Cancel: the saved note comes back and nothing is sent.
              noteSettled.current = true;
              setNote(line.render.instruction ?? "");
              e.currentTarget.blur();
              onFocusLine();
            }
          }}
          placeholder={DIRECTION_PLACEHOLDER}
        />
        <span className="hint">
          Ctrl+Enter saves, Esc cancels. Source: {line.render.direction_source}
        </span>
      </div>

      {tags.length > 0 && (
        <div className="field">
          <label htmlFor="ins-spoken">Inline events</label>
          <div className="row" style={{ gap: 4 }}>
            {tags.slice(0, 6).map((t) => (
              <button
                key={t}
                type="button"
                className={`chip round ${spoken.includes(t) ? "signal" : ""}`}
                onClick={() => insertTag(t)}
                title={`Insert ${t} into the spoken text`}
              >
                {t}
              </button>
            ))}
            {tags.length > 6 && <span className="chip round faint">+{tags.length - 6}</span>}
          </div>
          <textarea
            id="ins-spoken"
            ref={spokenRef}
            rows={3}
            value={spoken}
            onChange={(e) => setSpoken(e.target.value)}
            onBlur={() => {
              const next = spoken.trim();
              const current = line.render.spoken_text ?? line.text;
              if (next !== current) void onPatch({ spoken_text: next === line.text ? null : next });
            }}
          />
          <span className="hint">
            What the engine hears. The reader still sees the book's text.
          </span>
        </div>
      )}

      {/* ------------------------------------------------------------ QC */}
      <div className="field">
        <label htmlFor="ins-qc">
          QC note {line.flag && <span style={{ color: "var(--risk)" }}>· flagged</span>}
        </label>
        <textarea
          id="ins-qc"
          ref={qcRef}
          rows={2}
          value={qcNote}
          onChange={(e) => setQcNote(e.target.value)}
          onBlur={() => {
            if (qcNote !== (line.note ?? "")) void onPatch({ note: qcNote });
          }}
          placeholder="wrong speaker? name mispronounced?"
        />
        <div className="row" style={{ gap: 6 }}>
          <button
            className={`btn sm ${line.flag ? "on" : ""}`}
            aria-pressed={line.flag}
            onClick={() => void onPatch({ flag: !line.flag })}
          >
            {line.flag ? "⚑ Unflag" : "⚑ Flag (f)"}
          </button>
          <span className="hint">Never sent to an engine, never costs a render.</span>
        </div>
      </div>

      {/* --------------------------------------------------------- cache */}
      <div style={{ borderTop: "1px solid var(--rule)", paddingTop: 12 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="eyebrow" style={{ color: "var(--signal)" }}>
            Cache
          </span>
          <StateLabel state={line.audio_state} />
        </div>

        <p className="hint" style={{ marginTop: 6 }}>
          {line.audio_state === "stale" && (
            <>
              key <span className="mono">{shortKey(previousKey)}</span> → recomputed on the next
              render
            </>
          )}
          {line.audio_state === "cached" && (
            <>
              key <span className="mono">{shortKey(previousKey)}</span> ·{" "}
              {fmtDuration(line.duration_ms)}
            </>
          )}
          {line.audio_state === "missing" && <>never rendered</>}
        </p>

        <button
          className="btn"
          style={{ width: "100%", marginTop: 8 }}
          disabled={line.audio_state === "missing"}
          title="Play the take already on disk. No synth call."
          onClick={onPlay}
        >
          {auditioning ? "❚❚ Stop" : "▶ Play the cached take"}
        </button>
        <button
          className="btn signal"
          style={{ width: "100%", marginTop: 6 }}
          disabled={rendering}
          title="Synthesise this line again with its current settings."
          onClick={onRerender}
        >
          {rendering ? "Rendering…" : "↻ Re-render this line"}
        </button>
        <p className="hint" style={{ marginTop: 6 }}>
          Rest of the book untouched. A single-line render leaves the chapter's master audio to be
          rebuilt before export.
        </p>
        {playError && (
          <p className="hint" style={{ color: "var(--risk)" }}>
            {playError}
          </p>
        )}
      </div>
    </aside>
  );
}
