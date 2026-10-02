import { act, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { JobStreamHandlers } from "../api/types";
import { freshMockApi, renderWithStudio } from "../../test/renderWithStudio";
import { JobScreen } from "./JobScreen";

/**
 * The screen's side of the stream contract (jobStream.ts): it shows a
 * reconnect in progress, and when the subscription gives up it says so with a
 * Retry that opens a new subscription.
 */
async function screenWithControlledStream() {
  const mock = await freshMockApi();
  const [running] = await mock.listJobs();
  const job = await mock.getJob(running.id);
  const { log: _log, ...frame } = job;
  const subscriptions: JobStreamHandlers[] = [];
  const unsubscribed = vi.fn();
  const api = {
    ...mock,
    subscribeJob: vi.fn((_id: string, h: JobStreamHandlers) => {
      subscriptions.push(h);
      return unsubscribed;
    }),
  };
  const view = renderWithStudio(<JobScreen />, {
    route: `/jobs/${job.id}`,
    path: "/jobs/:id",
    api,
  });
  return { ...view, api, frame, subscriptions, unsubscribed };
}

describe("JobScreen and the job stream", () => {
  it("says when the stream is reconnecting, and clears it once frames flow again", async () => {
    const { subscriptions, frame } = await screenWithControlledStream();
    act(() => subscriptions[0].onProgress(frame));
    expect(await screen.findByRole("progressbar", { name: /Rendering/ })).toBeInTheDocument();

    act(() => subscriptions[0].onReconnecting?.(1, 750));
    expect(screen.getByText("live stream dropped, reconnecting…")).toBeInTheDocument();

    act(() => {
      subscriptions[0].onLive?.();
      subscriptions[0].onProgress({ ...frame, progress: { ...frame.progress, done: 2 } });
    });
    expect(screen.queryByText("live stream dropped, reconnecting…")).toBeNull();
  });

  it("shows why the stream gave up, and Retry opens a new subscription", async () => {
    const { user, api, subscriptions, unsubscribed, frame } = await screenWithControlledStream();
    act(() => subscriptions[0].onProgress(frame));
    await screen.findByRole("progressbar", { name: /Rendering/ });

    act(() => subscriptions[0].onGiveUp?.("no answer from the studio after 5 attempts (HTTP 502)"));
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(
      "Live updates stopped: no answer from the studio after 5 attempts (HTTP 502)",
    );

    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(api.subscribeJob).toHaveBeenCalledTimes(2);
    expect(unsubscribed).toHaveBeenCalledTimes(1); // the old subscription was closed first
    expect(screen.queryByText(/Live updates stopped/)).toBeNull();
    // The job already on screen stays while the new stream attaches.
    expect(screen.getByRole("progressbar", { name: /Rendering/ })).toBeInTheDocument();
  });

  it("offers Retry even before the first frame arrived", async () => {
    const { user, api, subscriptions } = await screenWithControlledStream();
    act(() => subscriptions[0].onGiveUp?.('No job "job_x"'));
    expect(screen.getByRole("alert")).toHaveTextContent('No job "job_x"');
    expect(screen.getByRole("link", { name: "Back to the shelf" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(api.subscribeJob).toHaveBeenCalledTimes(2);
    expect(screen.getByText("attaching to the job…")).toBeInTheDocument();
  });
});
