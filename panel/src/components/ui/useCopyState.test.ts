import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useCopyState } from "./useCopyState";

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubClipboard(writeText: (text: string) => Promise<void>) {
  vi.stubGlobal("isSecureContext", true);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
}

describe("useCopyState", () => {
  it("moves to copied on success, and back to idle after the pause", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    stubClipboard(() => Promise.resolve());
    const { result } = renderHook(() => useCopyState(500));
    expect(result.current.state).toBe("idle");

    await act(async () => {
      await result.current.copy("token");
    });
    expect(result.current.state).toBe("copied");

    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(result.current.state).toBe("idle");
    vi.useRealTimers();
  });

  it("moves to failed when the clipboard refuses, without throwing", async () => {
    stubClipboard(() => Promise.reject(new Error("denied")));
    const { result } = renderHook(() => useCopyState());
    await act(async () => {
      await result.current.copy("token");
    });
    expect(result.current.state).toBe("failed");
  });

  it("uses its own reset delay per instance", async () => {
    stubClipboard(() => Promise.resolve());
    const { result } = renderHook(() => useCopyState(50));
    await act(async () => {
      await result.current.copy("token");
    });
    expect(result.current.state).toBe("copied");
    await waitFor(() => {
      expect(result.current.state).toBe("idle");
    });
  });
});
