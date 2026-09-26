import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import { createQueryClient } from "./App";

describe("createQueryClient", () => {
  it("never retries a rate-limited query: the client already retried it once itself", () => {
    const client = createQueryClient();
    const retry = client.getDefaultOptions().queries?.retry;
    if (typeof retry !== "function") throw new Error("retry must be a function");
    const error = new ApiError(429, "rate_limited", "Too many requests.", null, null, 30);
    // Never retried, at any failure count: stacking TanStack's own retries on top of the
    // client's single Retry-After wait would turn one rate limit into a retry storm.
    expect(retry(0, error)).toBe(false);
    expect(retry(1, error)).toBe(false);
  });

  it("still retries an unreachable or failing server", () => {
    const client = createQueryClient();
    const retry = client.getDefaultOptions().queries?.retry;
    if (typeof retry !== "function") throw new Error("retry must be a function");
    const error = new ApiError(0, "network", "The request did not reach the server.");
    expect(retry(0, error)).toBe(true);
    expect(retry(1, error)).toBe(true);
    expect(retry(2, error)).toBe(false);
  });

  it("never retries a mutation", () => {
    const client = createQueryClient();
    expect(client.getDefaultOptions().mutations?.retry).toBe(false);
  });
});
