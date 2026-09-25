import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { CopyButton } from "./CopyButton";

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubClipboard(writeText: (text: string) => Promise<void>) {
  vi.stubGlobal("isSecureContext", true);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
}

describe("CopyButton", () => {
  it("copies the exact value and confirms it in words", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    stubClipboard(writeText);
    render(<CopyButton value="ssh root@arenna.dev" label="Copy SSH command" />);
    await userEvent.click(screen.getByRole("button", { name: "Copy SSH command" }));
    expect(writeText).toHaveBeenCalledWith("ssh root@arenna.dev");
    expect(screen.getByRole("status")).toHaveTextContent("Copied to clipboard");
  });

  it("computes a lazy value only when pressed", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    stubClipboard(writeText);
    const value = vi.fn(() => "line 1\nline 2");
    render(<CopyButton value={value} label="Copy output" />);
    expect(value).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Copy output" }));
    expect(writeText).toHaveBeenCalledWith("line 1\nline 2");
  });

  it("falls back to the legacy copy command outside a secure context", async () => {
    vi.stubGlobal("isSecureContext", false);
    const execCommand = vi.fn(() => true);
    Object.defineProperty(document, "execCommand", { configurable: true, value: execCommand });
    render(<CopyButton value="token" />);
    await userEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(screen.getByRole("status")).toHaveTextContent("Copied to clipboard");
  });

  it("says so when copying fails", async () => {
    stubClipboard(() => Promise.reject(new Error("denied")));
    render(<CopyButton value="token" />);
    await userEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(screen.getByRole("status")).toHaveTextContent("Copy failed");
  });

  it("returns to its resting state", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    stubClipboard(() => Promise.resolve());
    render(<CopyButton value="token" />);
    await userEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(screen.getByRole("status")).toHaveTextContent("Copied to clipboard");
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByRole("status")).toHaveTextContent("");
    vi.useRealTimers();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(<CopyButton value="token" label="Copy token" />);
    await expectNoAxeViolations(container);
  });
});
