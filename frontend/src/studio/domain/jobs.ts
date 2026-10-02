/**
 * Job statuses, defined once. The client's stream, the job screen, the jobs
 * list and the mock all ask the same three questions of a status; before this
 * each kept its own array.
 */
import type { JobStatus } from "../api/types";

/** Every status, in the contract's order (STUDIO-API.md, `JobSummary.status`). */
export const JOB_STATUSES = [
  "queued",
  "running",
  "paused",
  "done",
  "failed",
  "cancelled",
  "interrupted",
] as const satisfies readonly JobStatus[];

// If the export gains or drops a status, this line stops compiling.
type Missing = Exclude<JobStatus, (typeof JOB_STATUSES)[number]>;
const _exhaustive: [Missing] extends [never] ? true : Missing = true;
void _exhaustive;

/**
 * Finished for good: the stream closes after its `done` frame and nothing moves
 * again unless a person resumes a failed job. `interrupted` is not here — it is
 * a job the service lost in a restart, stopped but waiting to be resumed.
 */
export const TERMINAL_JOB_STATUSES = [
  "done",
  "failed",
  "cancelled",
] as const satisfies readonly JobStatus[];

/** Work is still expected to move (worth polling a list for). */
export const LIVE_JOB_STATUSES = [
  "queued",
  "running",
  "paused",
] as const satisfies readonly JobStatus[];

/** What `POST /jobs/{id}/resume` re-queues. */
export const RESUMABLE_JOB_STATUSES = [
  "interrupted",
  "failed",
  "paused",
] as const satisfies readonly JobStatus[];

const has = (set: readonly string[], status: string) => set.includes(status);

export const isTerminalJob = (status: JobStatus): boolean => has(TERMINAL_JOB_STATUSES, status);
export const isLiveJob = (status: JobStatus): boolean => has(LIVE_JOB_STATUSES, status);
export const isResumableJob = (status: JobStatus): boolean => has(RESUMABLE_JOB_STATUSES, status);
