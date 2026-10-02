/**
 * Type-level conformance: the UI's API types (./types.ts, ./client.ts) against
 * the types generated from the studio's OpenAPI export (./schema.gen.ts).
 *
 * Nothing here runs or is imported. `tsc --noEmit` (`npm run typecheck`, part
 * of `npm run check`) evaluates every `Expect<…>` below and fails when a check
 * is not `true`; the error names the check and the fields that drifted, e.g.
 *
 *   Type '{ drift: "the wire's value does not fit the UI type"; fields: "chapter"; }'
 *   does not satisfy the constraint 'true'.
 *
 * Directions:
 * - Responses: what the wire sends must be assignable to what the UI reads
 *   (Wire → UI), and a hand-written response type must name exactly the wire's
 *   fields, so a field added or dropped on either side fails too.
 * - Requests: what the UI sends must be assignable to what the wire accepts
 *   (UI → Wire), with no key the wire does not know (the studio forbids extras).
 * - Operations: each `StudioApi` method is tied to its route, so a route whose
 *   response model or body changes fails here even if the component names stay.
 *
 * Types in types.ts that are plain aliases of the schema cannot drift and are
 * not listed; the checks below cover every hand-written type, every response
 * the UI consumes and every request body it sends.
 */
import type { StudioApi } from "./client";
import type { JobFrame } from "./jobStream";
import type { components, paths } from "./schema.gen";
import type * as T from "./types";

type Schemas = components["schemas"];

// ---- the checking machinery ------------------------------------------------------

/** Compile error unless `Check` is exactly `true`. */
type Expect<Check extends true> = Check;

/**
 * `never` is assignable to everything, so a lookup that silently resolved to
 * `never` (a route with no JSON body, a method that is not async) would pass
 * every check below. Each check refuses it first.
 */
type Nothing = { drift: "nothing to compare: one side is never" };
type IsNever<X> = [X] extends [never] ? true : false;

/** Keys of A and B whose values do not flow from A to B. */
type Mismatched<From, To> = {
  [K in keyof From & keyof To]-?: [From[K]] extends [To[K]] ? never : K;
}[keyof From & keyof To];

/** Wire → UI, same field names. */
type Response<Wire, Ui> =
  IsNever<Wire> extends true
    ? Nothing
    : IsNever<Ui> extends true
      ? Nothing
      : [Wire] extends [Ui]
        ? [Exclude<keyof Wire, keyof Ui> | Exclude<keyof Ui, keyof Wire>] extends [never]
          ? true
          : {
              drift: "field names differ";
              onlyOnTheWire: Exclude<keyof Wire, keyof Ui>;
              onlyInTheUi: Exclude<keyof Ui, keyof Wire>;
            }
        : { drift: "the wire's value does not fit the UI type"; fields: Mismatched<Wire, Ui> };

/** UI → Wire, and no key the wire would refuse. */
type Request<Ui, Wire> =
  IsNever<Ui> extends true
    ? Nothing
    : IsNever<Wire> extends true
      ? Nothing
      : [Ui] extends [Wire]
        ? [Exclude<keyof Ui, keyof Wire>] extends [never]
          ? true
          : {
              drift: "the UI sends a key the wire does not accept";
              keys: Exclude<keyof Ui, keyof Wire>;
            }
        : { drift: "the UI sends a value the wire does not accept"; fields: Mismatched<Ui, Wire> };

/** Plain assignability, for arrays and whole payloads. */
type Flows<From, To> =
  IsNever<From> extends true
    ? Nothing
    : IsNever<To> extends true
      ? Nothing
      : [From] extends [To]
        ? true
        : { drift: "does not flow"; from: From; to: To };

// ---- operations --------------------------------------------------------------------

type Json<Part> = Part extends { content: { "application/json": infer Body } } ? Body : never;
type Success<Op> = Op extends { responses: infer R }
  ? R extends { 200: infer X }
    ? Json<X>
    : R extends { 201: infer X }
      ? Json<X>
      : R extends { 202: infer X }
        ? Json<X>
        : never
  : never;
type Body<Op> = Op extends { requestBody?: infer B } ? Json<NonNullable<B>> : never;
type ErrorBody<Op> = Op extends { responses: { default: infer X } } ? Json<X> : never;

type Returned<M extends keyof StudioApi> = StudioApi[M] extends (
  ...args: never[]
) => Promise<infer R>
  ? R
  : never;
type Arg<M extends keyof StudioApi, N extends number> = StudioApi[M] extends (
  ...args: infer A
) => unknown
  ? A[N]
  : never;

type Book = paths["/api/books/{slug}"];
type Lines = paths["/api/books/{slug}/lines/{line_id}"];
type Pass = paths["/api/books/{slug}/passes/{pass_id}"];

// ---- the checks ----------------------------------------------------------------------

export type ContractChecks = [
  // -- hand-written response types (wire → UI, same fields) --
  Expect<Response<Schemas["BookSummary"], T.BookSummary>>,
  Expect<Response<Schemas["Chapter"], T.Chapter>>,
  Expect<Response<Schemas["Script"], T.Script>>,
  Expect<Response<Schemas["JobSummary"], T.JobSummary>>,
  Expect<Response<Schemas["Job"], T.Job>>,
  // `measured`/`checks` are open maps on the wire; types.ts names the keys every
  // ACX row has (documented there). Everything else must match exactly.
  Expect<
    Response<
      Omit<Schemas["ExportFile"], "measured" | "checks">,
      Omit<T.ExportFile, "measured" | "checks">
    >
  >,
  Expect<Flows<keyof Schemas["ExportFile"], keyof T.ExportFile>>,
  Expect<Flows<keyof T.ExportFile, keyof Schemas["ExportFile"]>>,

  // -- request bodies (UI → wire) --
  Expect<Request<T.NewBookRequest, Schemas["NewBookRequest"]>>,
  Expect<Request<T.LinePatch, Schemas["LinePatch"]>>,
  Expect<Request<T.ChapterPatch, Schemas["ChapterPatch"]>>,
  Expect<Request<T.RenderRequest, Schemas["RenderRequest"]>>,
  Expect<Request<T.DirectRequest, Schemas["DirectRequest"]>>,
  Expect<Request<T.ExportRequest, Schemas["ExportRequest"]>>,
  Expect<Request<T.DecideRequest, Schemas["DecideRequest"]>>,
  Expect<Request<T.DecideRequest["decisions"][number], Schemas["Decision"]>>,
  Expect<Request<T.SettingsPatch, Schemas["SettingsPatch"]>>,
  Expect<Request<T.CastEntry, Schemas["CastPatchEntry"]>>,

  // -- the one error body real.ts parses --
  Expect<Response<Schemas["ErrorDetail"], { detail: string }>>,
  Expect<Flows<ErrorBody<Book["get"]>, { detail: string }>>,

  // -- SSE frames: the wire satisfies the zod validator, and what it passes on is what the UI reads --
  Expect<Flows<Omit<Schemas["Job"], "log">, JobFrame>>,
  Expect<Flows<JobFrame, T.JobProgress>>,
  Expect<Flows<Schemas["JobLogEntry"], T.JobLogEntry>>,

  // -- every StudioApi method against its route --
  Expect<Flows<Success<paths["/api/books"]["get"]>, Returned<"listBooks">>>,
  Expect<Flows<Success<paths["/api/books"]["post"]>, Returned<"createBook">>>,
  Expect<Request<Arg<"createBook", 0>, Body<paths["/api/books"]["post"]>>>,
  Expect<Flows<Success<paths["/api/books/upload"]["post"]>, Returned<"uploadBook">>>,
  Expect<Flows<Success<Book["get"]>, Returned<"getBook">>>,
  Expect<Flows<Success<paths["/api/books/{slug}/script"]["get"]>, Returned<"getScript">>>,
  Expect<Flows<Success<Lines["patch"]>, Returned<"patchLine">>>,
  Expect<Request<Arg<"patchLine", 2>, Body<Lines["patch"]>>>,
  Expect<
    Flows<Success<paths["/api/books/{slug}/chapters/{index}"]["patch"]>, Returned<"patchChapter">>
  >,
  Expect<
    Request<Arg<"patchChapter", 2>, Body<paths["/api/books/{slug}/chapters/{index}"]["patch"]>>
  >,
  Expect<
    Flows<
      Success<paths["/api/books/{slug}/lines/{line_id}/render"]["post"]>,
      Returned<"renderLine">
    >
  >,
  Expect<Flows<Success<paths["/api/books/{slug}/cast"]["get"]>, Returned<"getCast">>>,
  Expect<Flows<Success<paths["/api/books/{slug}/cast"]["patch"]>, Returned<"patchCast">>>,
  Expect<Flows<Arg<"patchCast", 1>, Body<paths["/api/books/{slug}/cast"]["patch"]>>>,
  Expect<Flows<Success<paths["/api/books/{slug}/cast/merge"]["post"]>, Returned<"mergeCast">>>,
  Expect<
    Request<
      { from: Arg<"mergeCast", 1>; into: Arg<"mergeCast", 2> },
      Body<paths["/api/books/{slug}/cast/merge"]["post"]>
    >
  >,
  Expect<Flows<Success<paths["/api/books/{slug}/lexicon"]["get"]>, Returned<"getLexicon">>>,
  Expect<Flows<Success<paths["/api/books/{slug}/lexicon"]["put"]>, Returned<"putLexicon">>>,
  Expect<
    Request<{ entries: Arg<"putLexicon", 1> }, Body<paths["/api/books/{slug}/lexicon"]["put"]>>
  >,
  Expect<Flows<Success<paths["/api/books/{slug}/versions"]["get"]>, Returned<"listVersions">>>,
  Expect<
    Flows<
      Success<paths["/api/books/{slug}/versions/{n}/restore"]["post"]>,
      Returned<"restoreVersion">
    >
  >,
  Expect<Flows<Success<paths["/api/books/{slug}/render"]["post"]>, Returned<"render">>>,
  Expect<Request<Arg<"render", 1>, Body<paths["/api/books/{slug}/render"]["post"]>>>,
  Expect<Flows<Success<paths["/api/books/{slug}/direct"]["post"]>, Returned<"direct">>>,
  Expect<Request<Arg<"direct", 1>, Body<paths["/api/books/{slug}/direct"]["post"]>>>,
  Expect<Flows<Success<paths["/api/books/{slug}/export"]["post"]>, Returned<"exportBook">>>,
  Expect<Request<Arg<"exportBook", 1>, Body<paths["/api/books/{slug}/export"]["post"]>>>,
  // (ExportFile's documented narrowing of `measured`/`checks`, as above)
  Expect<
    Flows<
      Omit<Success<paths["/api/books/{slug}/exports"]["get"]>[number], "measured" | "checks">,
      Omit<Returned<"listExports">[number], "measured" | "checks">
    >
  >,
  Expect<Flows<Success<paths["/api/books/{slug}/passes"]["get"]>, Returned<"listPasses">>>,
  Expect<Flows<Success<Pass["get"]>, Returned<"getPass">>>,
  Expect<
    Flows<Success<paths["/api/books/{slug}/passes/{pass_id}/decide"]["post"]>, Returned<"decide">>
  >,
  Expect<
    Request<Arg<"decide", 2>, Body<paths["/api/books/{slug}/passes/{pass_id}/decide"]["post"]>>
  >,
  Expect<
    Flows<Success<paths["/api/books/{slug}/passes/{pass_id}/apply"]["post"]>, Returned<"applyPass">>
  >,
  Expect<
    Flows<Success<paths["/api/books/{slug}/passes/{pass_id}/retry"]["post"]>, Returned<"retryPass">>
  >,
  Expect<
    Request<
      { scenes: Arg<"retryPass", 2> },
      Body<paths["/api/books/{slug}/passes/{pass_id}/retry"]["post"]>
    >
  >,
  Expect<Flows<Success<paths["/api/jobs"]["get"]>, Returned<"listJobs">>>,
  Expect<Flows<Success<paths["/api/jobs/{job_id}"]["get"]>, Returned<"getJob">>>,
  Expect<Flows<Success<paths["/api/jobs/{job_id}/pause"]["post"]>, Returned<"pauseJob">>>,
  Expect<Flows<Success<paths["/api/jobs/{job_id}/resume"]["post"]>, Returned<"resumeJob">>>,
  Expect<Flows<Success<paths["/api/jobs/{job_id}/cancel"]["post"]>, Returned<"cancelJob">>>,
  Expect<Flows<Success<paths["/api/settings"]["get"]>, Returned<"getSettings">>>,
  Expect<Flows<Success<paths["/api/settings"]["patch"]>, Returned<"patchSettings">>>,
  Expect<Request<Arg<"patchSettings", 0>, Body<paths["/api/settings"]["patch"]>>>,
  Expect<Flows<Success<paths["/api/health"]["get"]>, Returned<"health">>>,
  Expect<Flows<Success<paths["/api/settings"]["get"]>["engines"], Returned<"listEngines">>>,
];
