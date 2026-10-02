/**
 * Pure domain rules the screens share: no React, no I/O, no API calls. A screen
 * that needs one of these decisions imports it from here instead of keeping a
 * private copy (docs/FRONTEND-AUDIT-2026-09-26.md, Phase 3).
 */
export * from "./attribution";
export * from "./casting";
export * from "./jobs";
export * from "./render";
