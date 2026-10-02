import { useEffect, useState } from "react";

import type { StudioApi } from "../api/client";
import { errorMessage } from "../api/http";
import type { Health } from "../api/types";

/**
 * GET /health, shared by every screen.
 *
 * The pill lives in the top bar, which mounts on every route, so a naive
 * per-mount fetch would hit /health on every navigation. One module-level
 * cache, one timer, and every bar reads the same answer.
 */
export interface HealthState {
  health: Health | null;
  /** Set when the studio itself could not be reached. */
  error: string | null;
  loading: boolean;
}

const REFRESH_MS = 20_000;

let owner: StudioApi | null = null;
let state: HealthState = { health: null, error: null, loading: true };
let inFlight: Promise<void> | null = null;
let fetchedAt = 0;
let timer: number | null = null;
const subs = new Set<(s: HealthState) => void>();

function publish(next: HealthState) {
  state = next;
  for (const fn of subs) fn(state);
}

function refresh(api: StudioApi, force = false): void {
  if (inFlight) return;
  if (!force && Date.now() - fetchedAt < REFRESH_MS && state.health) return;
  inFlight = api
    .health()
    .then((h) => {
      fetchedAt = Date.now();
      publish({ health: h, error: null, loading: false });
    })
    .catch((e: unknown) => {
      fetchedAt = Date.now();
      publish({
        health: null,
        error: errorMessage(e),
        loading: false,
      });
    })
    .finally(() => {
      inFlight = null;
    });
}

/**
 * The current health, refreshed at most every 20 s while anything is mounted.
 * Switching between the mock and the real API resets the cache: the two do not
 * describe the same deployment.
 */
export function useHealth(api: StudioApi): HealthState {
  const [local, setLocal] = useState<HealthState>(() =>
    owner === api ? state : { health: null, error: null, loading: true },
  );

  useEffect(() => {
    if (owner !== api) {
      owner = api;
      fetchedAt = 0;
      inFlight = null;
      state = { health: null, error: null, loading: true };
    }
    subs.add(setLocal);
    setLocal(state);
    refresh(api);
    if (timer === null) {
      timer = window.setInterval(() => refresh(api, true), REFRESH_MS);
    }
    return () => {
      subs.delete(setLocal);
      if (subs.size === 0 && timer !== null) {
        window.clearInterval(timer);
        timer = null;
      }
    };
  }, [api]);

  return local;
}
