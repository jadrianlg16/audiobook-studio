import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { useApi } from "../api/client";
import { errorMessage } from "../api/http";
import type {
  Chapter,
  EngineOption,
  Line,
  LinePatch,
  PassSummary,
  QcStatus,
  Script,
} from "../api/types";
import {
  ATTRIBUTION_CONFIDENCE_FLOOR,
  needsSpeakerCheck,
  renderAction,
  UNKNOWN_SPEAKER,
} from "../domain";
import { EngineChip, ErrorBox, Spinner, useAsync } from "../ui/bits";
import { SHORTCUT_SCOPE, shortcutTargetAllowed } from "../ui/shortcuts";
import { qcStatusOf, SignOffButton } from "../ui/signoff";
import { TopBar } from "../ui/TopBar";
import { useStartJob } from "../ui/useStartJob";
import type { RowWindowHandle, VirtualRow } from "../ui/VirtualRows";
import { VIRTUALISE_ABOVE, VirtualRows } from "../ui/VirtualRows";
import type { ChapterStat } from "./ChapterRail";
import { ChapterRail } from "./ChapterRail";
import { ExportsPanel, VersionsPanel } from "./DeskPanels";
import { Inspector } from "./Inspector";
import { LineRow } from "./LineRow";
import { Transport, usePlayhead } from "./Transport";

interface DeskData {
  script: Script;
  engines: EngineOption[];
  passes: PassSummary[];
}

type Filter = "all" | "stale" | "undirected" | "flagged" | "unknown";

const FILTERS: { id: Filter; label: string; title: string }[] = [
  { id: "all", label: "all", title: "Every line in the chapter" },
  { id: "stale", label: "stale", title: "Lines whose audio does not match their current settings" },
  { id: "undirected", label: "undirected", title: "Lines with no performance note" },
  { id: "flagged", label: "flagged", title: "Lines you flagged for a pickup (f)" },
  {
    id: "unknown",
    label: "unknown speaker",
    title: `Speaker ${UNKNOWN_SPEAKER}, or attributed with confidence below ${ATTRIBUTION_CONFIDENCE_FLOOR}`,
  },
];

function matchesFilter(l: Line, filter: Filter, speaker: string): boolean {
  if (speaker && l.speaker !== speaker) return false;
  switch (filter) {
    case "stale":
      return l.audio_state !== "cached";
    case "undirected":
      return !l.render.instruction;
    case "flagged":
      return l.flag;
    case "unknown":
      return needsSpeakerCheck(l);
    default:
      return true;
  }
}

/**
 * A first guess at a row's height, used only until it has been measured once.
 * Text wraps at roughly 62 characters in this column; a note or a QC note is
 * another line each.
 */
function estimateLineHeight(line: Line): number {
  const wraps = Math.floor(line.text.length / 62);
  return 78 + wraps * 21 + (line.render.instruction ? 20 : 0) + (line.note ? 18 : 0);
}

/**
 * The Director's Desk. The script is the document; direction sits under
 * each line the way a parenthetical sits in a screenplay. Editing a note flips
 * exactly that line to stale, and the header offers to render just those.
 */
export function Desk() {
  const api = useApi();
  const { slug = "" } = useParams();
  const job = useStartJob();
  const [params, setParams] = useSearchParams();

  const load = useAsync<DeskData>(
    async (signal) => {
      const [script, engines, passes] = await Promise.all([
        api.getScript(slug, signal),
        // Engine metadata is a garnish. A cold gateway or a studio hiccup here
        // must not take down a script that loaded perfectly well.
        api.listEngines(signal).catch(() => [] as EngineOption[]),
        api.listPasses(slug, signal).catch(() => [] as PassSummary[]),
      ]);
      return { script, engines, passes };
    },
    [api, slug],
  );

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [playError, setPlayError] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);
  const [filter, setFilter] = useState<Filter>("all");
  const [speakerFilter, setSpeakerFilter] = useState("");
  const [panel, setPanel] = useState<"exports" | "versions" | null>(null);
  const [auditionId, setAuditionId] = useState<string | null>(null);
  /**
   * Chapters whose master audio this session left behind: a single-line render
   * caches the line but does not re-stitch the chapter WAV, and `POST
   * /lines/{id}/render` returns only the line. Tracking it here keeps the
   * header honest until the script is fetched again.
   */
  const [restitch, setRestitch] = useState<number[]>([]);
  const rowsRef = useRef<RowWindowHandle | null>(null);
  const auditionRef = useRef<HTMLAudioElement | null>(null);

  const script = load.data?.script ?? null;
  const chapters = useMemo(() => script?.chapters ?? [], [script]);

  // Chapter indices are 0-based. A missing or unparseable ?chapter is absent,
  // not 1 — the old default landed every multi-chapter book on chapter two.
  const raw = params.get("chapter");
  const wanted =
    raw === null || raw.trim() === "" || !Number.isFinite(Number(raw)) ? null : Number(raw);
  const chapter =
    (wanted !== null ? chapters.find((c) => c.index === wanted) : undefined) ?? chapters[0] ?? null;

  const setChapter = useCallback(
    (index: number) => {
      // Keep every other query param (?mock=1 among them).
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set("chapter", String(index));
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  const linesByChapter = useMemo(() => {
    const map = new Map<number, Line[]>();
    for (const l of script?.lines ?? []) {
      const arr = map.get(l.chapter_index) ?? [];
      arr.push(l);
      map.set(l.chapter_index, arr);
    }
    return map;
  }, [script]);

  const chapterIndex = chapter?.index ?? -1;
  const chapterLines = useMemo(
    () => linesByChapter.get(chapterIndex) ?? [],
    [linesByChapter, chapterIndex],
  );
  /** One map, built once — the row loop used to do a linear scan per line. */
  const lineById = useMemo(() => {
    const m = new Map<string, Line>();
    for (const l of chapterLines) m.set(l.id, l);
    return m;
  }, [chapterLines]);

  const filtering = filter !== "all" || speakerFilter !== "";
  const visibleIds = useMemo(() => {
    const s = new Set<string>();
    for (const l of chapterLines) if (matchesFilter(l, filter, speakerFilter)) s.add(l.id);
    return s;
  }, [chapterLines, filter, speakerFilter]);

  /** Every count on this screen comes from here, so they cannot disagree. */
  const stats = useMemo(() => {
    const per = new Map<number, ChapterStat>();
    let staleLines = 0;
    const staleChapters = new Set<number>();
    const included = new Set(chapters.filter((c) => c.include_in_audio).map((c) => c.index));
    for (const c of chapters) {
      per.set(c.index, { directed: 0, total: 0, stale: 0, missing: 0, matches: 0 });
      if (c.include_in_audio && !c.fresh) staleChapters.add(c.index);
    }
    for (const i of restitch) if (included.has(i)) staleChapters.add(i);
    for (const l of script?.lines ?? []) {
      const s = per.get(l.chapter_index);
      if (!s) continue;
      s.total++;
      if (l.render.instruction) s.directed++;
      if (l.audio_state === "stale") s.stale++;
      if (l.audio_state === "missing") s.missing++;
      if (matchesFilter(l, filter, speakerFilter)) s.matches = (s.matches ?? 0) + 1;
      // A skipped chapter is not rendered, so offering to render its lines
      // would be a button that does nothing.
      if (l.audio_state !== "cached" && included.has(l.chapter_index)) {
        staleLines++;
        staleChapters.add(l.chapter_index);
      }
    }
    return {
      per,
      staleLines,
      staleChapters: [...staleChapters].filter((i) => included.has(i)).length,
    };
  }, [script, chapters, filter, speakerFilter, restitch]);

  /**
   * The script as a flat row list: one row per scene rule, one per visible line.
   * Only the descriptors are built here — the elements are created by
   * `renderRow`, for the rows in the window only, so selecting a line does not
   * rebuild 1,200 elements.
   */
  const rows = useMemo<VirtualRow[]>(() => {
    const out: VirtualRow[] = [];
    for (const scene of chapter?.scenes ?? []) {
      const ids = scene.line_ids.filter((id) => visibleIds.has(id));
      if (ids.length === 0) continue;
      out.push({ key: scene.id, kind: "scene", estimate: 34 });
      for (const id of ids) {
        const line = lineById.get(id);
        if (line) out.push({ key: line.id, kind: "line", estimate: estimateLineHeight(line) });
      }
    }
    return out;
  }, [chapter, visibleIds, lineById]);

  const chapterDuration = useMemo(
    () =>
      chapterLines.reduce(
        (acc, l) => Math.max(acc, l.audio ? l.audio.end_ms : 0),
        chapter?.duration_ms ?? 0,
      ),
    [chapterLines, chapter],
  );

  const audioSrc = chapter?.audio_file && slug ? api.bookFileUrl(slug, chapter.audio_file) : null;
  const head = usePlayhead(audioSrc, chapterDuration);

  const playingLineId = useMemo(() => {
    if (!head.playing && head.ms === 0) return null;
    const hit = chapterLines.find(
      (l) => l.audio && head.ms >= l.audio.start_ms && head.ms < l.audio.end_ms,
    );
    return hit?.id ?? null;
  }, [chapterLines, head.ms, head.playing]);

  // Keep the playing line in view while following. This goes through the row
  // window, not querySelector: on a long chapter the line being spoken may not
  // be in the DOM yet, and the window has to scroll to it before it can be.
  useEffect(() => {
    if (!follow || !playingLineId) return;
    rowsRef.current?.scrollToKey(playingLineId);
  }, [playingLineId, follow]);

  const selected = selectedId ? (lineById.get(selectedId) ?? null) : null;

  /**
   * Open on the first line the reader can actually see. With a filter on, the
   * old version selected the chapter's first line even when it was filtered
   * out, so the inspector edited a row that was nowhere on screen.
   */
  useEffect(() => {
    if (chapterLines.length === 0) return;
    const shown = chapterLines.filter((l) => visibleIds.has(l.id));
    const pool = shown.length > 0 ? shown : chapterLines;
    if (!selectedId || !pool.some((l) => l.id === selectedId)) setSelectedId(pool[0].id);
  }, [chapterLines, visibleIds, selectedId]);

  /**
   * Put focus back on a row after an edit. The row window does the work: with a
   * virtualised chapter the element may need scrolling into existence first.
   */
  const focusRow = useCallback((lineId: string) => {
    rowsRef.current?.focusKey(lineId);
  }, []);

  const replaceLine = useCallback((updated: Line) => {
    load.set((prev) =>
      prev
        ? {
            ...prev,
            script: {
              ...prev.script,
              lines: prev.script.lines.map((l) => (l.id === updated.id ? updated : l)),
            },
          }
        : prev,
    );
    // load.set is stable (a React setState)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * The script's version and the book summary (the header's "directed" count,
   * the directable lines, the engines) as the server last stated them. A
   * mutation answers with the line or chapter it changed, not the book, so
   * after one the version is taken from the answer when it carries it and the
   * summary is read again. Versions only grow, so the highest seen wins; of the
   * summaries, the newest read wins. Nothing lands on another book's script.
   */
  const setDeskData = load.set;
  const applyServerState = useCallback(
    (forSlug: string, next: { version?: number; book?: Script["book"] }) => {
      setDeskData((prev) => {
        if (!prev || prev.script.book.slug !== forSlug) return prev;
        const version = Math.max(prev.script.version, next.version ?? 0);
        const book = next.book ?? prev.script.book;
        if (version === prev.script.version && book === prev.script.book) return prev;
        return { ...prev, script: { ...prev.script, version, book } };
      });
    },
    [setDeskData],
  );

  const summarySeq = useRef(0);
  /** Read the book summary again; `withVersion` when the mutation's answer had no version. */
  const refreshSummary = useCallback(
    async (withVersion: boolean) => {
      const mine = ++summarySeq.current;
      try {
        const [book, versions] = await Promise.all([
          api.getBook(slug),
          withVersion ? api.listVersions(slug) : Promise.resolve([]),
        ]);
        const version = versions.reduce((max, v) => Math.max(max, v.version), 0);
        applyServerState(slug, mine === summarySeq.current ? { version, book } : { version });
      } catch {
        // The edit itself landed and is on screen; a missed recount is not
        // worth an error box.
      }
    },
    [api, slug, applyServerState],
  );

  /**
   * One in-flight PATCH per line wins: an older response arriving late used to
   * overwrite whatever the user had typed since.
   */
  const patchSeq = useRef(new Map<string, number>());
  const patchLine = useCallback(
    async (lineId: string, patch: LinePatch) => {
      setActionError(null);
      const mine = (patchSeq.current.get(lineId) ?? 0) + 1;
      patchSeq.current.set(lineId, mine);
      try {
        const res = await api.patchLine(slug, lineId, patch);
        if (patchSeq.current.get(lineId) !== mine) return; // superseded
        replaceLine(res.line);
        applyServerState(slug, { version: res.version });
        void refreshSummary(false);
      } catch (e) {
        if (patchSeq.current.get(lineId) !== mine) return;
        setActionError(errorMessage(e));
      }
    },
    [api, slug, replaceLine, applyServerState, refreshSummary],
  );

  const rerenderLine = useCallback(
    async (lineId: string) => {
      setBusy(true);
      setPlayError(null);
      try {
        const res = await api.renderLine(slug, lineId);
        replaceLine(res.line);
        setRestitch((prev) =>
          prev.includes(res.line.chapter_index) ? prev : [...prev, res.line.chapter_index],
        );
        const el = new Audio(res.audio_url);
        el.addEventListener("error", () =>
          setPlayError("Rendered, but the audio could not be played from here."),
        );
        await el
          .play()
          .catch(() => setPlayError("Rendered, but the audio could not be played from here."));
      } catch (e) {
        setActionError(errorMessage(e));
      } finally {
        setBusy(false);
      }
    },
    [api, slug, replaceLine],
  );

  // Read through refs so these callbacks stay identical between renders and
  // React.memo(LineRow) can actually hold.
  const auditionIdRef = useRef(auditionId);
  auditionIdRef.current = auditionId;

  /** Audition the take already on disk. No synth call, no cost. */
  const playLine = useCallback(
    (line: Line) => {
      setPlayError(null);
      auditionRef.current?.pause();
      if (auditionIdRef.current === line.id) {
        setAuditionId(null);
        return;
      }
      const el = new Audio(api.lineAudioUrl(slug, line.id));
      auditionRef.current = el;
      setAuditionId(line.id);
      el.addEventListener("ended", () => setAuditionId(null));
      el.addEventListener("error", () => {
        setAuditionId(null);
        setPlayError("No cached audio for this line. Render it, then it plays from the cache.");
      });
      void el.play().catch(() => {
        setAuditionId(null);
        setPlayError("No cached audio for this line. Render it, then it plays from the cache.");
      });
    },
    [api, slug],
  );

  useEffect(() => () => auditionRef.current?.pause(), []);

  const toggleFlag = useCallback(
    (line: Line) => void patchLine(line.id, { flag: !line.flag }),
    [patchLine],
  );

  const replaceChapter = useCallback((updated: Chapter) => {
    load.set((prev) =>
      prev
        ? {
            ...prev,
            script: {
              ...prev.script,
              chapters: prev.script.chapters.map((x) => (x.index === updated.index ? updated : x)),
            },
          }
        : prev,
    );
    // load.set is stable (a React setState)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggleInclude = useCallback(
    async (c: Chapter) => {
      setBusy(true);
      setActionError(null);
      try {
        replaceChapter(
          await api.patchChapter(slug, c.index, { include_in_audio: !c.include_in_audio }),
        );
        // Saved as a new version, which the Chapter it answers with does not carry.
        void refreshSummary(true);
      } catch (e) {
        setActionError(errorMessage(e));
      } finally {
        setBusy(false);
      }
    },
    [api, slug, replaceChapter, refreshSummary],
  );

  /**
   * Chapter sign-off (v1.2). It is a sidecar field: no new script version, no
   * cache churn, so it is safe to click while a render is queued. A studio that
   * answers without the field keeps the value we just asked for rather than
   * silently reverting to "unheard" — an outright 422 is what says the
   * deployment does not have it.
   */
  const setSignOff = useCallback(
    async (c: Chapter, next: QcStatus) => {
      setActionError(null);
      try {
        const updated = await api.patchChapter(slug, c.index, { qc_status: next });
        replaceChapter({ ...updated, qc_status: updated.qc_status ?? next });
      } catch (e) {
        setActionError(errorMessage(e));
      }
    },
    [api, slug, replaceChapter],
  );

  /**
   * The open row editor, readable synchronously. Ending an edit puts focus back
   * on the row, which blurs the textarea, and its blur handler commits: without
   * this, Esc saved the draft it was cancelling and Ctrl+Enter saved it twice.
   * Whichever of commit or cancel runs first closes the edit, and the blur it
   * causes then finds nothing open.
   */
  const editingRef = useRef<string | null>(null);

  const beginEdit = useCallback((line: Line) => {
    editingRef.current = line.id;
    setSelectedId(line.id);
    setEditingId(line.id);
    setDraft(line.render.instruction ?? "");
  }, []);

  /** Close the editor on `line`. False when it was not open. */
  const endEdit = useCallback((line: Line) => {
    if (editingRef.current !== line.id) return false;
    editingRef.current = null;
    setEditingId(null);
    return true;
  }, []);

  const draftRef = useRef(draft);
  draftRef.current = draft;

  const commitEdit = useCallback(
    (line: Line) => {
      if (!endEdit(line)) return;
      focusRow(line.id); // the textarea is about to unmount; don't drop focus
      const next = draftRef.current.trim();
      if (next === (line.render.instruction ?? "")) return;
      void patchLine(line.id, { instruction: next || null });
    },
    [endEdit, focusRow, patchLine],
  );

  const cancelEdit = useCallback(
    (line: Line) => {
      if (!endEdit(line)) return;
      focusRow(line.id);
    },
    [endEdit, focusRow],
  );

  const headRef = useRef(head);
  headRef.current = head;
  const selectLine = useCallback((line: Line) => {
    setSelectedId(line.id);
    if (line.audio) headRef.current.seek(line.audio.start_ms);
  }, []);

  // j / k move between lines, f flags one, Shift+A signs the chapter off. They
  // act only from the page, the script or the rail, never on a focused control
  // (shortcutTargetAllowed). The listener is registered once: it reads the
  // current lines through a ref, because chapterLines is a new array on every
  // render.
  const navRef = useRef({
    lines: chapterLines,
    selectedId,
    visible: visibleIds,
    filtering,
    chapter,
  });
  navRef.current = { lines: chapterLines, selectedId, visible: visibleIds, filtering, chapter };
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      // Approval takes Shift, so a stray `a` cannot sign a chapter off.
      const approve = e.shiftKey && e.key.toLowerCase() === "a";
      if (!approve && !["j", "k", "f"].includes(e.key)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (!shortcutTargetAllowed(e.target)) return;
      const {
        lines,
        selectedId: current,
        visible,
        filtering: isFiltered,
        chapter: ch,
      } = navRef.current;

      // Shift+A is about the chapter, not the line, so it works even in an
      // empty filter result. A skipped chapter has no audio to sign off.
      if (approve) {
        if (!ch || !ch.include_in_audio) return;
        e.preventDefault();
        // Approve; pressing it again on an approved chapter takes it back to
        // unheard, so the shortcut can undo itself.
        void setSignOff(ch, qcStatusOf(ch) === "approved" ? "unheard" : "approved");
        return;
      }

      const walk = isFiltered ? lines.filter((l) => visible.has(l.id)) : lines;
      if (walk.length === 0) return;
      if (e.key === "f") {
        const line = walk.find((l) => l.id === current);
        if (!line) return;
        e.preventDefault();
        void patchLine(line.id, { flag: !line.flag });
        return;
      }
      const idx = walk.findIndex((l) => l.id === current);
      const next =
        e.key === "j" ? Math.min(walk.length - 1, idx + 1) : Math.max(0, idx <= 0 ? 0 : idx - 1);
      const line = walk[idx === -1 ? 0 : next];
      if (!line) return;
      e.preventDefault();
      setSelectedId(line.id);
      rowsRef.current?.scrollToKey(line.id);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [patchLine, setSignOff]);

  if (load.loading) {
    return (
      <>
        <TopBar crumbs={<span className="mono">{slug}</span>} />
        <div className="page">
          <Spinner label="opening the script" />
        </div>
      </>
    );
  }
  if (load.error || !script || !chapter) {
    return (
      <>
        <TopBar crumbs={<span className="mono">{slug}</span>} />
        <div className="page">
          <ErrorBox error={load.error ?? "This book has no chapters."} onRetry={load.reload} />
          <p style={{ marginTop: 12 }}>
            <Link className="btn" to="/">
              Back to the shelf
            </Link>
          </p>
        </div>
      </>
    );
  }

  const book = script.book;
  // The contract promises no order for GET /passes, so pick the newest here.
  const latestPass =
    [...(load.data?.passes ?? [])].sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0] ??
    null;
  const engineIds = Object.keys(book.engines);
  const directableLines = book.directable?.lines ?? 0;
  const chapterStat = stats.per.get(chapter.index);
  const speakers = [
    ...new Set([...script.cast.map((c) => c.character), ...script.lines.map((l) => l.speaker)]),
  ].sort();

  const renderStale = renderAction({ lines: stats.staleLines, chapters: stats.staleChapters });

  return (
    <>
      <TopBar
        crumbs={
          <>
            <Link to="/">{book.title}</Link>
            <span className="faint">/</span>
            <span>
              Chapter {chapter.index + 1} · {chapter.title}
            </span>
            {chapter.include_in_audio ? (
              <SignOffButton
                status={qcStatusOf(chapter)}
                chapterNumber={chapter.index + 1}
                onCycle={(next) => void setSignOff(chapter, next)}
              />
            ) : (
              <span className="chip">skipped</span>
            )}
          </>
        }
      >
        {engineIds.map((id) => (
          <EngineChip
            key={id}
            engine={id}
            option={load.data?.engines.find((e) => e.id === id)}
            licenceNote={book.licence_note}
          />
        ))}
        <span className="chip">
          {book.utterances} ln · {book.directed.directed} directed
        </span>
        {latestPass && (
          <Link className="btn sm" to={`/books/${slug}/review/${latestPass.id}`}>
            Pass review
          </Link>
        )}
        <button
          className="btn sm"
          disabled={busy || job.starting !== null || directableLines === 0}
          title={
            directableLines === 0
              ? "No line in this book is on an engine that takes a written note, so a pass would have nothing to write to."
              : `Write scene notes for the ${directableLines} lines that can use them.`
          }
          onClick={() => void job.start("direct", () => api.direct(slug, { scope: "all" }))}
        >
          {job.isStarting("direct") ? "Starting…" : "Direct this book"}
        </button>
        <button
          className="btn signal"
          disabled={busy || job.starting !== null || renderStale.kind === "none"}
          title={renderStale.title}
          onClick={() => void job.start("stale", () => api.render(slug, { scope: "stale" }))}
        >
          {job.isStarting("stale") ? "Starting…" : `▶ ${renderStale.label}`}
        </button>
      </TopBar>

      {(actionError || job.error) && (
        <div style={{ padding: "8px 16px 0" }}>
          <ErrorBox error={actionError ?? job.error ?? ""} />
        </div>
      )}

      {directableLines === 0 && (
        <div className="notice" style={{ margin: "8px 16px 0" }}>
          No line in this book is on an engine that takes a written note; notes are saved but change
          nothing until you route lines to one.{" "}
          <Link to={`/books/${slug}/cast`}>Open the cast sheet</Link> to recast a character.
        </div>
      )}

      <div className="desk">
        <ChapterRail
          slug={slug}
          chapters={chapters}
          currentIndex={chapter.index}
          stats={stats.per}
          cast={script.cast}
          licenceNote={book.licence_note}
          filtering={filtering}
          busy={busy}
          panel={panel}
          onSelect={setChapter}
          onToggleInclude={(c) => void toggleInclude(c)}
          onSignOff={(c, next) => void setSignOff(c, next)}
          onPanel={setPanel}
        />

        {/* -------------------------------------------------------- script */}
        <main className="script" {...SHORTCUT_SCOPE}>
          <div className="scripthead">
            <span className="eyebrow">Script</span>
            <span className="mono faint" style={{ fontSize: 10.5 }}>
              {chapterStat?.total ?? 0} lines · {chapterStat?.directed ?? 0} directed
              {filtering && ` · ${chapterStat?.matches ?? 0} shown`}
            </span>
            <span className="spacer" />
            <span className="eyebrow">State</span>
          </div>

          <div className="filterbar" role="group" aria-label="Filter the script">
            {FILTERS.map((f) => (
              <button
                key={f.id}
                className={`btn sm ${filter === f.id ? "on" : ""}`}
                aria-pressed={filter === f.id}
                title={f.title}
                onClick={() => setFilter(f.id)}
              >
                {f.label}
              </button>
            ))}
            <label className="sronly" htmlFor="desk-speaker">
              Filter by character
            </label>
            <select
              id="desk-speaker"
              value={speakerFilter}
              onChange={(e) => setSpeakerFilter(e.target.value)}
              title="Only this character's lines"
            >
              <option value="">every character</option>
              {speakers.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            {filtering && (
              <button
                className="btn sm ghost"
                onClick={() => {
                  setFilter("all");
                  setSpeakerFilter("");
                }}
              >
                clear
              </button>
            )}
            <span className="hint" style={{ marginLeft: "auto" }}>
              j/k move · f flags · Shift+A signs off
              {rows.length > VIRTUALISE_ABOVE && (
                <span
                  title={`${rows.length} rows: only the visible window is in the page. Browser find (Ctrl+F) sees the visible rows; use the filters to narrow the chapter instead.`}
                >
                  {" "}
                  · windowed
                </span>
              )}
            </span>
          </div>

          {panel === "exports" && <ExportsPanel slug={slug} onClose={() => setPanel(null)} />}
          {panel === "versions" && (
            <VersionsPanel
              slug={slug}
              currentVersion={script.version}
              onClose={() => setPanel(null)}
              onRestored={(next) => load.set((prev) => (prev ? { ...prev, script: next } : prev))}
            />
          )}

          <VirtualRows
            rows={rows}
            handleRef={rowsRef}
            renderRow={(row) => {
              if (row.kind === "scene") {
                return (
                  <div className="scene-rule">
                    <span className="mono" style={{ fontSize: 10 }}>
                      {row.key}
                    </span>
                  </div>
                );
              }
              const line = lineById.get(row.key);
              if (!line) return null;
              const editing = editingId === line.id;
              return (
                <LineRow
                  line={line}
                  selected={selectedId === line.id}
                  playing={playingLineId === line.id}
                  auditioning={auditionId === line.id}
                  editing={editing}
                  draft={editing ? draft : ""}
                  onSelect={selectLine}
                  onBeginEdit={beginEdit}
                  onDraft={setDraft}
                  onCommit={commitEdit}
                  onCancelEdit={cancelEdit}
                  onPlay={playLine}
                  onToggleFlag={toggleFlag}
                />
              );
            }}
          />

          {filtering && (chapterStat?.matches ?? 0) === 0 && (
            <p className="hint" style={{ padding: "18px 2px" }}>
              No line in this chapter matches that filter. Other chapters may still have hits — the
              rail counts them.
            </p>
          )}

          <Transport
            head={head}
            totalMs={chapterDuration}
            follow={follow}
            onFollowChange={setFollow}
            chapterLabel={`Chapter ${chapter.index + 1}`}
            label={chapter.audio_file ?? "not rendered"}
          />
        </main>

        {/* ----------------------------------------------------- inspector */}
        <Inspector
          line={selected}
          cast={script.cast}
          engines={load.data?.engines ?? []}
          rendering={busy}
          auditioning={auditionId === selected?.id}
          playError={playError}
          onPatch={(patch) => (selected ? patchLine(selected.id, patch) : Promise.resolve())}
          onFocusLine={() => selected && focusRow(selected.id)}
          onRerender={() => selected && void rerenderLine(selected.id)}
          onPlay={() => selected && playLine(selected)}
        />
      </div>
    </>
  );
}
