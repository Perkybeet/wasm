import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createElement } from "react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../../app/App";
import { applyServerEvent } from "../../realtime/events";
import { fakeBackend, json } from "../../test/fakes";
import { JOB_FOLLOW_POLL_MS, isJobFinished, jobFollowInterval, jobKeys, jobQuery, keepFinishedJob, useFollowedJob } from "./jobs";
import type { Job } from "./jobs";

const RUNNING: Job = {
  id: "j1",
  type: "update",
  name: "Update shop.example.com",
  description: "Updating shop.example.com",
  status: "running",
  progress: 60,
  total_steps: 100,
  current_step: "Building",
  created_at: "2026-09-25T19:21:13+00:00",
  logs: [],
  metadata: { domain: "shop.example.com" },
};
const COMPLETED: Job = { ...RUNNING, status: "completed", progress: 100, current_step: "Done" };

describe("isJobFinished", () => {
  it("is true for the three ends and nothing else", () => {
    expect(["completed", "failed", "cancelled"].every((status) => isJobFinished({ status }))).toBe(true);
    expect(["pending", "running"].some((status) => isJobFinished({ status }))).toBe(false);
  });
});

describe("keepFinishedJob", () => {
  it("never lets an ended job read as running again", () => {
    expect(keepFinishedJob(COMPLETED, RUNNING)).toBe(COMPLETED);
    expect(keepFinishedJob({ ...RUNNING, status: "failed" }, { ...RUNNING, status: "pending" })).toMatchObject({ status: "failed" });
  });

  it("takes every forward move, and a new job under the same key", () => {
    expect(keepFinishedJob(RUNNING, COMPLETED)).toEqual(COMPLETED);
    expect(keepFinishedJob(RUNNING, { ...RUNNING, progress: 80 })).toMatchObject({ progress: 80 });
    expect(keepFinishedJob(COMPLETED, { ...RUNNING, id: "j2" })).toMatchObject({ id: "j2", status: "running" });
  });

  it("leaves anything that is not a job to the usual structural sharing", () => {
    const log = { content: "a" };
    expect(keepFinishedJob(log, { content: "a" })).toBe(log);
    expect(keepFinishedJob(undefined, COMPLETED)).toEqual(COMPLETED);
  });
});

describe("following a job", () => {
  it("keeps the end the event reported when a slower fetch answers with the job still running", async () => {
    const client = createQueryClient();
    // The fetch left while the job ran; its last event (completed) lands before the answer.
    await client.query({
      ...jobQuery("j1"),
      queryFn: () => {
        applyServerEvent(client, "job", COMPLETED);
        return Promise.resolve(RUNNING);
      },
    });
    expect(client.getQueryData<Job>(jobKeys.detail("j1"))?.status).toBe("completed");
  });

  it("guards entries an event creates before any page asks for the job", () => {
    const client = createQueryClient();
    applyServerEvent(client, "job", COMPLETED);
    client.setQueryData(jobKeys.detail("j1"), RUNNING);
    expect(client.getQueryData<Job>(jobKeys.detail("j1"))?.status).toBe("completed");
  });

  it("reads a running job again until it ends, and stops then", () => {
    expect(jobFollowInterval(undefined)).toBe(JOB_FOLLOW_POLL_MS);
    expect(jobFollowInterval(RUNNING)).toBe(JOB_FOLLOW_POLL_MS);
    expect(jobFollowInterval(COMPLETED)).toBe(false);
  });
});

describe("useFollowedJob", () => {
  function wrapper(client = createQueryClient()) {
    return {
      client,
      wrapper: ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children),
    };
  }

  it("follows the job the response queued, and a newer event wins over that snapshot", async () => {
    const { client, wrapper: Wrapper } = wrapper();
    // The job's own event can beat the response that queued it.
    applyServerEvent(client, "job", { ...RUNNING, progress: 90 });
    const { result } = renderHook(() => useFollowedJob(), { wrapper: Wrapper });
    act(() => {
      result.current.follow({ ...RUNNING, status: "pending", progress: 0 });
    });
    expect(result.current.id).toBe("j1");
    expect(result.current.job?.progress).toBe(90);

    act(() => {
      applyServerEvent(client, "job", COMPLETED);
    });
    // Query notifies observers on the next tick.
    await waitFor(() => {
      expect(result.current.job?.status).toBe("completed");
    });
    // A late snapshot of the job still running changes nothing.
    act(() => {
      client.setQueryData(jobKeys.detail("j1"), RUNNING);
    });
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(result.current.job?.status).toBe("completed");

    act(() => {
      result.current.dismiss();
    });
    expect(result.current.job).toBeNull();
  });

  it("reads the job when only its id is known", async () => {
    fakeBackend({ "GET /api/jobs/j1": () => json(200, COMPLETED) });
    const { wrapper: Wrapper } = wrapper();
    const { result } = renderHook(() => useFollowedJob(), { wrapper: Wrapper });
    act(() => {
      result.current.follow("j1");
    });
    await waitFor(() => {
      expect(result.current.job?.status).toBe("completed");
    });
  });
});
