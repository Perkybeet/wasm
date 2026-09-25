import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { RelativeTime } from "./RelativeTime";

describe("RelativeTime", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    vi.setSystemTime(new Date(2026, 8, 25, 12, 0, 0));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("says how long ago, with the machine-readable moment in dateTime", () => {
    const moment = new Date(2026, 8, 25, 11, 57, 0);
    render(<RelativeTime value={moment} />);
    const time = screen.getByText("3m ago");
    expect(time.tagName).toBe("TIME");
    expect(time).toHaveAttribute("dateTime", moment.toISOString());
  });

  it("reads the store's naive timestamps", () => {
    render(<RelativeTime value="2026-09-25T11:00:00.378313" />);
    expect(screen.getByText("1h ago")).toBeInTheDocument();
  });

  it("keeps itself current while on screen", () => {
    render(<RelativeTime value={new Date(2026, 8, 25, 11, 59, 30)} />);
    expect(screen.getByText("30s ago")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(screen.getByText("35s ago")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByText("1m ago")).toBeInTheDocument();
  });

  it("shows a timestamp it cannot place verbatim instead of guessing", () => {
    render(<RelativeTime value="Fri 2026-09-25 13:06:35 CEST" />);
    expect(screen.getByText("Fri 2026-09-25 13:06:35 CEST")).toHaveClass("mono");
  });

  it("says the fallback when there is no time at all", () => {
    render(<RelativeTime value={null} fallback="Never deployed" />);
    expect(screen.getByText("Never deployed")).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    vi.useRealTimers();
    const { container } = render(
      <p>
        Deployed <RelativeTime value={new Date(Date.now() - 90_000)} />
      </p>,
    );
    await expectNoAxeViolations(container);
  });
});
