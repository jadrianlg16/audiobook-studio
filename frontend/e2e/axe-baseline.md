# Accessibility baseline (axe)

Written by `AXE_BASELINE_UPDATE=1 npm run e2e` (e2e/axe-baseline.ts) — do not edit by hand.
Mock mode,
Playwright's Desktop Chrome viewport (1280×720), axe-core 4.13.0, tags
`wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa`, `wcag22aa`, `best-practice`.

The e2e run fails on any **critical** violation not listed under Known
critical below. Phase 4 of docs/FRONTEND-AUDIT-2026-09-26.md tightens it to
**serious**, so the Critical and Serious columns have to reach zero. The
impact columns count violated rules; numbers in brackets are affected nodes.
The full axe results for each route are attached to its test in the
Playwright report (and saved under test-results/).

| Route | Path | Critical | Serious | Moderate | Minor | Critical + serious rules (nodes) |
|---|---|---:|---:|---:|---:|---|
| shelf | `/` | 0 | 0 | 2 | 0 | — |
| new-book | `/books/new` | 0 | 1 | 2 | 0 | `target-size` (2) |
| desk-cold-lantern | `/books/cold-lantern` | 0 | 1 | 2 | 0 | `target-size` (5) |
| cast-cold-lantern | `/books/cold-lantern/cast` | 0 | 0 | 2 | 0 | — |
| lexicon-cold-lantern | `/books/cold-lantern/lexicon` | 0 | 0 | 2 | 1 | — |
| desk-harbour-road | `/books/harbour-road` | 0 | 1 | 2 | 0 | `target-size` (3) |
| cast-harbour-road | `/books/harbour-road/cast` | 0 | 0 | 2 | 0 | — |
| lexicon-harbour-road | `/books/harbour-road/lexicon` | 0 | 0 | 2 | 1 | — |
| desk-fog-signals | `/books/fog-signals` | 0 | 1 | 2 | 0 | `target-size` (2) |
| cast-fog-signals | `/books/fog-signals/cast` | 0 | 0 | 2 | 0 | — |
| lexicon-fog-signals | `/books/fog-signals/lexicon` | 0 | 0 | 2 | 1 | — |
| desk-long-watch | `/books/long-watch` | 0 | 1 | 2 | 0 | `target-size` (2) |
| cast-long-watch | `/books/long-watch/cast` | 0 | 0 | 2 | 0 | — |
| lexicon-long-watch | `/books/long-watch/lexicon` | 0 | 0 | 2 | 1 | — |
| review-pass_9f2ca41 | `/books/cold-lantern/review/pass_9f2ca41` | 0 | 1 | 3 | 0 | `color-contrast` (7) |
| jobs | `/jobs` | 0 | 0 | 2 | 0 | — |
| job-job_ren_001 | `/jobs/job_ren_001` | 0 | 0 | 3 | 0 | — |
| job-job_ren_002 | `/jobs/job_ren_002` | 0 | 0 | 3 | 0 | — |
| job-job_ren_003 | `/jobs/job_ren_003` | 0 | 0 | 3 | 0 | — |
| settings | `/settings` | 0 | 0 | 2 | 0 | — |
| lab | `/lab` | 2 | 1 | 2 | 0 | `label` (2), `select-name` (1), `color-contrast` (3) |

## Known critical (tolerated until Phase 4)

- **lab**: `label` (2): Form elements must have labels; `select-name` (1): Select element must have an accessible name.

The reasons are next to each entry in `KNOWN_CRITICAL` (e2e/axe-baseline.ts).

## Serious rules

| Rule | What it checks | Routes |
|---|---|---|
| `color-contrast` | Elements must meet minimum color contrast ratio thresholds | review-pass_9f2ca41, lab |
| `target-size` | All touch targets must be 24px large, or leave sufficient space | new-book, desk-cold-lantern, desk-harbour-road, desk-fog-signals, desk-long-watch |
