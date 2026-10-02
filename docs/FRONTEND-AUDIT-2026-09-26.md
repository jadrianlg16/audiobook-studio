# Frontend audit — 2026-09-26

A professional-baseline review of `frontend/`, done before the hardening work it
plans. Three sources: a live run of the UI in mock mode (axe-core, font-size
census, viewport sweeps), an architecture read of the code, and a design-system /
accessibility read of the code. Every finding below cites the code or the measurement
that produced it (line numbers refer to the code as it was at the time).

## Baseline (measured 2026-09-26)

| What | Value | How |
|---|---|---|
| Typecheck | passes | `npx tsc --noEmit` → exit 0 |
| Production bundle | 311 kB main chunk (96.8 kB gzip), mock split out (39 kB) | `vite build` |
| Frontend tests | 0 | no `*.test.*` / `*.spec.*` outside node_modules |
| Lint / format config | none (4 `eslint-disable` comments for a linter that isn't installed) | file search |
| CI | none (no `.github/`) | |
| Error boundaries | 0 | grep |
| Inline `style={{}}` objects | 165 | grep |
| Desk text at ≤ 12 px | 161 of 175 text nodes | computed style census, 1440×900 |
| Shelf text at ≤ 12 px | 106 of 151 text nodes | same |
| Distinct font sizes | 18, no scale | code read |
| axe (Shelf) | no `<main>`, 43 nodes outside landmarks | axe-core 4.10, wcag2a/aa/21aa/22aa + best-practice |
| axe (Desk) | no `h1` (no headings at all), 5 targets < 24 px | same |
| Text contrast | passes (`--ink` ≈16.7:1, `--ink-faint` ≥4.66:1) | WCAG luminance on the tokens |
| Control borders | 1.31:1 (`--rule` on `--panel`), needs 3:1 | same |

## Bugs (fix first, each with a regression test)

1. **Esc saves instead of cancelling, in both note editors.** Inline row editor:
   `cancelEdit` → `focusRow` moves focus synchronously → the textarea blurs →
   `onBlur={() => onCommit(line)}` saves the typed draft (`Desk.tsx:447-464`,
   `LineRow.tsx:98`). Inspector: `setNote(old)` then `blur()` runs the *current*
   `onBlur` closure, which still holds the typed text (`Inspector.tsx:252-264`).
   Reproduced in mock: versions 7→8 and 10→11.
2. **Ctrl+Enter in the Inspector PATCHes twice** — `onPatch` then `blur()` → `onBlur`
   PATCHes again because the line prop is not updated yet (`Inspector.tsx:256-259`).
   Reproduced: 8→10.
3. **Global `a` approves the chapter while focus is on any button or the scrub slider**
   — the handler skips only INPUT/TEXTAREA/SELECT (`Desk.tsx:486-488`). WCAG 2.1.4.
4. **≤ 760 px wide, the windowed script goes blank on scroll.** `.desk` becomes the
   scroller but `VirtualRows` listens to `.script`; after scrolling 20 000 px, 0 rows in
   the viewport, 16 in the DOM (The Long Watch, mock).
5. **Upload drops `casting_mode`** — `NewBook.tsx:45-52` never appends it; the backend
   accepts it (`pipeline/studio/api.py:151`).
6. **Version/"directed" counters drift after edits** — `Desk.tsx:309-311` discards
   `res.version`; the Versions panel said "current v7" after four new versions; the
   header chip `book.directed` is never refreshed.

## Gaps against a professional baseline

- **Safety net:** no tests, lint, format, CI or error boundary.
- **Server state:** hand-rolled `useAsync` (`ui/bits.tsx:156-197`) aborts and orders
  well but has no cache, dedupe or invalidation — hence bug 6, the double
  `GET /settings` (`SettingsScreen.tsx:21-23`), the 4 s full book-list refetch on Jobs,
  and CastSheet discarding the `Script` that `patchCast`/`mergeCast` return.
- **Types:** hand-written mirror of `docs/STUDIO-API.md`; FastAPI already serves
  OpenAPI but 6 JSON routes lack a `response_model` (PATCH line, render line, the four
  passes routes). Responses and SSE frames are cast, never checked.
- **Resilience:** the job stream never reconnects (one SSE error → polling forever,
  `real.ts:244-249`), the poller has no in-flight guard, no Retry after 5 failures;
  21 copies of `e instanceof Error ? e.message : String(e)`; errors lose the status.
- **Components:** `Desk.tsx` 824 lines; ~10 button styles, 5 badge styles, empty states
  built inline; no Button/Field/PageHeader/EmptyState primitives.
- **Styling:** three systems — `studio.css` (tokens, good), 165 inline styles, and
  Tailwind used only by `/lab` whose global reset silently props up the studio;
  `studio.css` is unlayered, so it overrides Tailwind utilities. No type or spacing
  scale; three disabled opacities; amber (`--signal`) means six things.
- **Accessibility:** script rows are `div onClick tabIndex={-1}` — `j`/`k` moves the
  selection but not focus, no `aria-selected`; no `document.title` per route, no focus
  move on navigation, no skip link, `<main>` only on the Desk; placeholder-only or
  missing labels (`Review.tsx:318`, `lab/App.tsx:442`, `LineRow.tsx:91`,
  `Inspector.tsx:120`, `SettingsScreen.tsx:249`); job `aria-live` announces the ETA
  every tick; 49 explanations only in `title=` tooltips; no unsaved-change guard.
- **Layout:** at 1024 px the script pane shrinks to ~1.5 visible lines; below 1080 px
  the inspector becomes a one-column bottom sheet.
- **Performance:** fine for a local tool; every route and the lab ship in one chunk;
  the playhead re-renders the whole Desk 4×/s.

## Kept on purpose (do not break)

Strict TypeScript with zero `any`; colour tokens with light/dark values and documented
contrast; the global `:focus-visible` ring; reduced-motion in CSS and JS; the
`VirtualRows` windowed list (variable heights, anchoring, `scrollToKey`/`focusKey`);
the `StudioApi` interface injected by context with the mock lazily split and not sticky
in production; `ConfirmButton`; per-line PATCH sequencing.

## Decisions

- UI language stays **English** (no i18n for now).
- **One styling system:** the token CSS. Tailwind is removed; its preflight becomes an
  explicit, owned reset; the lab moves onto the studio tokens and primitives.
- **TanStack Query** owns server state; the `StudioApi` interface stays the only
  transport, so the mock keeps working.
- **Types are checked against the backend:** the studio's OpenAPI is exported to
  `docs/studio-openapi.json`, `openapi-typescript` generates a schema, and a
  type-level conformance file fails `tsc` on drift.
- **E2E runs on the mock**, in a real browser via Playwright. Locally it uses the
  installed Chrome (`PW_CHANNEL`, default `chrome`); CI installs Chromium.

## Plan and acceptance

| Phase | Scope | Done when |
|---|---|---|
| 0 Safety net | Vitest + Testing Library + jsdom, ESLint (typescript-eslint, react-hooks, jsx-a11y) + Prettier, route error boundaries, Playwright smoke per route with axe, `npm run check`, GitHub Actions workflow | `npm run check` green; a thrown render error shows the boundary, not a blank page |
| 1 Bugs | the six bugs above | each has a test that fails on the old code and passes on the new |
| 3 Architecture | TanStack Query; OpenAPI export + generated schema + conformance; `ApiError`; SSE reconnect with backoff + serialized polls + Retry; split `Desk.tsx` into hooks + pure `domain/`; lab under `src/lab/`; lazy routes; shared constants | no screen holds a private copy of server data; mutations update/invalidate; `tsc` fails on a contract drift |
| 2 Design system | owned reset (zero visual diff before intentional changes), rem type scale with a 12 px floor, spacing/radius/opacity tokens, primitives (Button, IconButton, Field, Badge, PageHeader, EmptyState, Toolbar), inline styles → classes, status colours split, control borders ≥ 3:1, `color-scheme`, lab on tokens, layouts at 1024 px and ≤ 760 px | no text under 12 px; inline styles only for truly dynamic values; one button family |
| 4 Accessibility & UX | script as a listbox with roving focus and `aria-selected`; shortcuts scoped to script/rail with an off switch, approval on Shift+A; per-route title, `h1`, `<main>`, skip link, focus on navigation; labels; live regions for milestones only; unsaved-change guards; confirm Cancel job / Reject all; visible disabled reasons; Space = play/pause; 24 px targets | axe clean (no serious/critical) on every route in e2e; full Desk loop by keyboard only |

Order: 0 (with the backend OpenAPI export alongside it) → 1 → 3 → 2 → 4, because every
phase touches the Desk.

## Status

- **Done: phase 0.** Vitest + Testing Library, ESLint + Prettier, route error
  boundaries, Playwright smoke with axe on every route, `npm run check`, and the CI
  workflow.
- **Done: phase 1.** Each of the six bugs has its own fix and regression test.
- **Done: most of phase 3.** `ApiError`, SSE reconnect with backoff, serialized
  polls and Retry, shared rules in `src/studio/domain/`, the lab under `src/lab/`,
  lazy routes, and the OpenAPI export with generated types and a `tsc` conformance
  check. **Not done:** TanStack Query; server state is still the hand-rolled
  `useAsync`.
- **Not started: phase 2 (design system) and phase 4 (accessibility and UX).**
  Tailwind is still in use by `/lab`, and axe in e2e still gates on *critical*
  violations only (see `frontend/e2e/axe-baseline.md`).
