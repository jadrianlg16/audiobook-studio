import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { MutableRefObject, ReactNode } from "react";

import { prefersReducedMotion } from "./bits";

/**
 * A row descriptor, deliberately *not* a rendered node: the caller builds the
 * whole list of these (cheap: three fields) and `renderRow` is called only for
 * the rows in the window, so selecting a line does not re-create 1,200
 * elements.
 */
export interface VirtualRow {
  /** Stable identity: the line id, or the scene id for a scene rule. */
  key: string;
  /** Rows of a kind are averaged together while estimating unmeasured rows. */
  kind: string;
  /** Height guess used until the row has been measured once. */
  estimate: number;
}

export interface RowWindowHandle {
  /** Scroll a row into view (block: nearest), whether or not it is rendered. */
  scrollToKey: (key: string, smooth?: boolean) => void;
  /** Scroll it into view, then put DOM focus on it once it exists. */
  focusKey: (key: string) => void;
  /** True when rows outside the viewport are not in the DOM. */
  virtualised: boolean;
}

/** Rows above this count render in full: a small book gets the simpler DOM. */
export const VIRTUALISE_ABOVE = 150;

/** Render this much above and below the viewport so a fast scroll stays filled. */
const OVERSCAN_PX = 900;

/**
 * The transport is `position: sticky; bottom: 0` inside the same scroller, so a
 * row scrolled to the very bottom would sit under it. CSS pixels, not a
 * deployment value.
 */
const PAD_BOTTOM = 84;
const PAD_TOP = 8;

/**
 * A jump longer than this many viewports is instant rather than smooth. On the
 * way, a long smooth scroll renders rows that were never measured, and the
 * anchor correction that keeps content still (`scrollTop += drift`) cancels a
 * smooth scroll in Chrome — `j` from 20,000 px down stopped about twenty lines
 * short of its target.
 */
const SMOOTH_MAX_VIEWPORTS = 2;

/**
 * After an instant jump, frames spent re-aiming at the row while the rows
 * around it are measured and the offsets above it settle.
 */
const SETTLE_FRAMES = 8;

function buildOffsets(rows: VirtualRow[], sizeOf: (row: VirtualRow) => number): Float64Array {
  const off = new Float64Array(rows.length + 1);
  for (let i = 0; i < rows.length; i++) off[i + 1] = off[i] + sizeOf(rows[i]);
  return off;
}

/**
 * The element that scrolls `el`: its nearest ancestor with a scrolling
 * overflow, else the document's scrolling element.
 */
function scrollParentOf(el: HTMLElement): HTMLElement {
  for (let node = el.parentElement; node; node = node.parentElement) {
    const { overflowY } = getComputedStyle(node);
    if (overflowY === "auto" || overflowY === "scroll" || overflowY === "overlay") return node;
  }
  return (document.scrollingElement as HTMLElement | null) ?? document.documentElement;
}

/**
 * The document's scroller reports its box in page coordinates and fires its
 * scroll events on the window; every other scroller is its own viewport.
 */
const isDocumentScroller = (s: HTMLElement) =>
  s === document.scrollingElement || s === document.documentElement;
const viewportTop = (s: HTMLElement) => (isDocumentScroller(s) ? 0 : s.getBoundingClientRect().top);
const scrollEventsOf = (s: HTMLElement): HTMLElement | Window =>
  isDocumentScroller(s) ? window : s;

/** Largest i with offsets[i] <= y, clamped into the list. */
function indexAt(offsets: Float64Array, y: number, count: number): number {
  if (count === 0) return 0;
  let lo = 0;
  let hi = count - 1;
  if (y <= 0) return 0;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (offsets[mid] <= y) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

/**
 * A windowed list for the script column.
 *
 * A chapter of 1,200 lines is 1,200 rows, each with a textarea-capable note
 * button, a play button and a flag button: rendering them all costs a
 * multi-second first paint and makes every keystroke in the inspector re-layout
 * the whole column. This renders only the rows in (and just around) the
 * viewport.
 *
 * It is written here rather than pulled from react-window because the desk
 * needs three things a fixed-size list does not give: rows of genuinely
 * variable height (a line with a note is twice a line without one), scrolling a
 * row that is *not currently rendered* into view for `j`/`k` and follow-script,
 * and putting DOM focus back on a row after an edit. All three are one method
 * on the handle below.
 *
 * The scroller is not this element but an ancestor, and which one depends on
 * the layout: on a wide window the desk's script column scrolls on its own; at
 * 760 px and below the whole desk is one scroller (studio.css). A window
 * measured against an element that does not scroll is the wrong window, so the
 * scroller is found from the DOM — the nearest ancestor that scrolls — and
 * found again whenever the window is resized. Offsets are measured relative to
 * the list's own top inside that scroller.
 */
export function VirtualRows({
  rows,
  renderRow,
  handleRef,
  threshold = VIRTUALISE_ABOVE,
  className,
}: {
  rows: VirtualRow[];
  renderRow: (row: VirtualRow) => ReactNode;
  handleRef?: MutableRefObject<RowWindowHandle | null>;
  threshold?: number;
  className?: string;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  const heights = useRef(new Map<string, number>());
  /** Running mean per row kind, so an unmeasured row is not guessed blindly. */
  const kindStats = useRef(new Map<string, { sum: number; n: number }>());
  const [sizeVersion, setSizeVersion] = useState(0);
  const [range, setRange] = useState({ start: 0, end: 0 });
  /** The element that scrolls this list under the current layout. */
  const [scroller, setScroller] = useState<HTMLElement | null>(null);

  useLayoutEffect(() => {
    const resolve = () => {
      const list = listRef.current;
      // Same element, same state: React skips the render.
      if (list) setScroller(scrollParentOf(list));
    };
    resolve();
    window.addEventListener("resize", resolve);
    return () => window.removeEventListener("resize", resolve);
  }, []);

  const virtual = rows.length > threshold;

  const sizeOf = useCallback((row: VirtualRow) => {
    const measured = heights.current.get(row.key);
    if (measured !== undefined) return measured;
    const stat = kindStats.current.get(row.kind);
    return stat && stat.n > 0 ? stat.sum / stat.n : row.estimate;
  }, []);

  const offsets = useMemo(
    () => buildOffsets(rows, sizeOf),
    // sizeVersion is the measurement generation: new measurements, new offsets.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rows, sizeVersion, sizeOf],
  );
  const total = offsets[rows.length] ?? 0;

  const indexByKey = useMemo(() => {
    const m = new Map<string, number>();
    rows.forEach((r, i) => m.set(r.key, i));
    return m;
  }, [rows]);

  /** Distance from the scroller's content top down to this list's top. */
  const listTop = useCallback(() => {
    const list = listRef.current;
    if (!scroller || !list) return 0;
    return list.getBoundingClientRect().top - viewportTop(scroller) + scroller.scrollTop;
  }, [scroller]);

  const recomputeRange = useCallback(() => {
    if (!scroller || !virtual) return;
    const top = scroller.scrollTop - listTop() - OVERSCAN_PX;
    const bottom = scroller.scrollTop + scroller.clientHeight - listTop() + OVERSCAN_PX;
    const start = indexAt(offsets, top, rows.length);
    const end = Math.min(rows.length, indexAt(offsets, bottom, rows.length) + 1);
    setRange((prev) => (prev.start === start && prev.end === end ? prev : { start, end }));
  }, [offsets, rows.length, scroller, virtual, listTop]);

  // A stable listener that reads the latest recompute through a ref, so the
  // scroll subscription is not torn down and rebuilt on every measurement.
  const recomputeRef = useRef(recomputeRange);
  recomputeRef.current = recomputeRange;

  useEffect(() => {
    if (!scroller || !virtual) return;
    const events = scrollEventsOf(scroller);
    let raf = 0;
    const onScroll = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        recomputeRef.current();
      });
    };
    events.addEventListener("scroll", onScroll, { passive: true });
    // The document's scroller does not resize with the window; the window does.
    window.addEventListener("resize", onScroll);
    const ro = new ResizeObserver(onScroll);
    ro.observe(scroller);
    recomputeRef.current();
    return () => {
      events.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
      ro.disconnect();
      if (raf) cancelAnimationFrame(raf);
    };
  }, [scroller, virtual]);

  // A new row set (chapter change, filter change) starts at the top of the list.
  useEffect(() => {
    if (virtual) recomputeRef.current();
  }, [rows, virtual]);

  /**
   * Measure what was just rendered. Rows above the viewport keep their
   * estimated height until they are rendered, so scrolling *up* into unmeasured
   * rows would otherwise shift the content the reader is looking at: the
   * anchor correction below puts the scroll position back where it was.
   *
   * No dependency array on purpose: it runs after every render, because any
   * render can change what is in the DOM. `changed` ends the loop — the second
   * pass measures the same heights and sets nothing.
   */
  // eslint-disable-next-line react-hooks/exhaustive-deps -- measures every render, see above
  useLayoutEffect(() => {
    if (!virtual) return;
    const list = listRef.current;
    if (!list || !scroller) return;

    let changed = false;
    for (const el of Array.from(list.querySelectorAll<HTMLElement>("[data-vkey]"))) {
      const key = el.dataset.vkey;
      if (!key) continue;
      const h = el.getBoundingClientRect().height;
      if (h <= 0) continue;
      const prev = heights.current.get(key);
      if (prev !== undefined && Math.abs(prev - h) <= 0.5) continue;
      if (prev === undefined) {
        const row = rows[indexByKey.get(key) ?? -1];
        if (row) {
          const stat = kindStats.current.get(row.kind) ?? { sum: 0, n: 0 };
          stat.sum += h;
          stat.n += 1;
          kindStats.current.set(row.kind, stat);
        }
      }
      heights.current.set(key, h);
      changed = true;
    }
    if (!changed) return;

    const anchor = indexAt(offsets, scroller.scrollTop - listTop(), rows.length);
    const next = buildOffsets(rows, sizeOf);
    const drift = next[anchor] - offsets[anchor];
    if (Math.abs(drift) > 0.5) scroller.scrollTop += drift;
    setSizeVersion((v) => v + 1);
  });

  /**
   * Where the scroller has to be for `key` to sit in view (block: nearest), or
   * null when it already does or is not in the list. Read through a ref so a
   * settle loop running across frames aims with the latest measurements.
   */
  const targetFor = (key: string): number | null => {
    const i = indexByKey.get(key);
    if (!scroller || i === undefined) return null;
    const base = listTop();
    const top = base + offsets[i];
    const bottom = base + offsets[i + 1];
    const viewTop = scroller.scrollTop;
    const viewBottom = viewTop + scroller.clientHeight;
    if (top < viewTop + PAD_TOP) return Math.max(0, top - PAD_TOP);
    if (bottom > viewBottom - PAD_BOTTOM) return bottom - scroller.clientHeight + PAD_BOTTOM;
    return null;
  };
  const targetRef = useRef(targetFor);
  targetRef.current = targetFor;
  /** Bumped by every scroll request, so an older settle loop stops. */
  const aimRef = useRef(0);

  const scrollToKey = useCallback(
    (key: string, smooth = true) => {
      const list = listRef.current;
      if (!list) return;
      const aim = ++aimRef.current;

      if (!virtual) {
        const behavior: ScrollBehavior = smooth && !prefersReducedMotion() ? "smooth" : "auto";
        const el = list.querySelector(`[data-vkey="${CSS.escape(key)}"]`);
        el?.scrollIntoView({ block: "nearest", behavior });
        return;
      }
      if (!scroller) return;
      const next = targetRef.current(key);
      if (next === null) return;
      const far =
        Math.abs(next - scroller.scrollTop) > scroller.clientHeight * SMOOTH_MAX_VIEWPORTS;
      const behavior: ScrollBehavior =
        smooth && !far && !prefersReducedMotion() ? "smooth" : "auto";
      scroller.scrollTo({ top: next, behavior });
      // Render the destination immediately rather than waiting for the scroll
      // event, so a `focusKey` right after this finds the row.
      recomputeRef.current();
      if (behavior === "smooth") return;

      // The jump landed on estimated offsets; the rows it rendered are measured
      // next frame and the offsets above the row can move. Re-aim until the
      // row sits in view or the frames run out.
      const settle = (left: number) => {
        if (left === 0 || aim !== aimRef.current) return;
        requestAnimationFrame(() => {
          if (aim !== aimRef.current) return;
          const again = targetRef.current(key);
          if (again === null) return;
          scroller.scrollTop = again;
          recomputeRef.current();
          settle(left - 1);
        });
      };
      settle(SETTLE_FRAMES);
    },
    [scroller, virtual],
  );

  const focusKey = useCallback(
    (key: string) => {
      scrollToKey(key, false);
      const attempt = (left: number) => {
        const el = listRef.current?.querySelector<HTMLElement>(
          `[data-vkey="${CSS.escape(key)}"] [data-line]`,
        );
        if (el) el.focus();
        else if (left > 0) requestAnimationFrame(() => attempt(left - 1));
      };
      attempt(3);
    },
    [scrollToKey],
  );

  useLayoutEffect(() => {
    if (!handleRef) return;
    handleRef.current = { scrollToKey, focusKey, virtualised: virtual };
    return () => {
      handleRef.current = null;
    };
  }, [handleRef, scrollToKey, focusKey, virtual]);

  const start = virtual ? range.start : 0;
  const end = virtual ? Math.max(range.end, range.start) : rows.length;
  const slice = rows.slice(start, end);

  return (
    <div className={`vrows${className ? ` ${className}` : ""}`} ref={listRef}>
      {virtual && start > 0 && <div style={{ height: offsets[start] }} aria-hidden />}
      {slice.map((row) => (
        <div className="vrow" data-vkey={row.key} key={row.key}>
          {renderRow(row)}
        </div>
      ))}
      {virtual && end < rows.length && (
        <div style={{ height: Math.max(0, total - offsets[end]) }} aria-hidden />
      )}
    </div>
  );
}
