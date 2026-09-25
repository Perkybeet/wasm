import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Announcer } from "../../app/Announcer";
import { useAnnounceChange } from "./useAnnounceChange";

function Watcher({ state }: { state: string | null }) {
  useAnnounceChange(state, state === null ? null : `picconia.com: ${state}`, state === "failed" ? "assertive" : "polite");
  return null;
}

describe("useAnnounceChange", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function settle() {
    act(() => {
      vi.advanceTimersByTime(150);
    });
  }

  it("says nothing for the first value: a page loading is not news", () => {
    render(
      <>
        <Announcer />
        <Watcher state="running" />
      </>,
    );
    settle();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("says each change, failures assertively", () => {
    const { rerender } = render(
      <>
        <Announcer />
        <Watcher state={null} />
      </>,
    );
    rerender(
      <>
        <Announcer />
        <Watcher state="running" />
      </>,
    );
    rerender(
      <>
        <Announcer />
        <Watcher state="deploying" />
      </>,
    );
    settle();
    expect(screen.getByRole("status")).toHaveTextContent("picconia.com: deploying");
    rerender(
      <>
        <Announcer />
        <Watcher state="failed" />
      </>,
    );
    settle();
    expect(screen.getByRole("alert")).toHaveTextContent("picconia.com: failed");
  });
});
