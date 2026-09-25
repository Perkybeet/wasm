import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Announcer, announce } from "./Announcer";

describe("Announcer", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("mounts both live regions empty", () => {
    render(<Announcer />);
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(screen.getByRole("alert")).toBeEmptyDOMElement();
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("alert")).toHaveAttribute("aria-live", "assertive");
  });

  it("says transitions politely and failures assertively, then clears", () => {
    render(<Announcer />);
    act(() => {
      announce("Deploy of shop.example.com started");
      announce("Deploy of api.example.com failed", "assertive");
      vi.advanceTimersByTime(150);
    });
    expect(screen.getByRole("status")).toHaveTextContent("Deploy of shop.example.com started");
    expect(screen.getByRole("alert")).toHaveTextContent("Deploy of api.example.com failed");
    act(() => {
      vi.advanceTimersByTime(8_000);
    });
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("repeats the same sentence when it is said twice", () => {
    render(<Announcer />);
    act(() => {
      announce("Saved");
      vi.advanceTimersByTime(150);
      announce("Saved");
    });
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    act(() => {
      vi.advanceTimersByTime(150);
    });
    expect(screen.getByRole("status")).toHaveTextContent("Saved");
  });
});
