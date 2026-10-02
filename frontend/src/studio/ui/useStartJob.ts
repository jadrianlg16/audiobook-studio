import { useCallback, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { errorMessage } from "../api/http";
import type { Job } from "../api/types";

/**
 * Starting a job is the same gesture everywhere: disable the button, POST,
 * navigate to the job screen, and — the part that was quietly different between
 * the desk and the shelf — keep the error on screen when it fails instead of
 * navigating to nothing.
 *
 * `key` identifies which button is starting, so a shelf with five books can
 * disable exactly the one that was pressed. On success the hook navigates and
 * deliberately leaves `starting` set: the screen is going away, and clearing it
 * would flash the button back to life first.
 */
export interface StartJobState {
  /** The key currently starting, or null. */
  starting: string | null;
  isStarting: (key: string) => boolean;
  error: string | null;
  clearError: () => void;
  start: (key: string, run: () => Promise<Job>) => Promise<void>;
}

export function useStartJob(): StartJobState {
  const navigate = useNavigate();
  const [starting, setStarting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);

  const start = useCallback(
    async (key: string, run: () => Promise<Job>) => {
      if (busy.current) return; // a second click while the POST is in flight
      busy.current = true;
      setStarting(key);
      setError(null);
      try {
        const job = await run();
        navigate(`/jobs/${job.id}`);
      } catch (e) {
        setError(errorMessage(e));
        setStarting(null);
      } finally {
        busy.current = false;
      }
    },
    [navigate],
  );

  const isStarting = useCallback((key: string) => starting === key, [starting]);
  const clearError = useCallback(() => setError(null), []);

  return { starting, isStarting, error, clearError, start };
}
