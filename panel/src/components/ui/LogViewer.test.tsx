import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import type { LogLine } from "./LogViewer";
import { LogViewer } from "./LogViewer";

const ESC = "\u001b";

function lines(count: number, text = (i: number) => `line ${String(i)}`): LogLine[] {
  return Array.from({ length: count }, (_, i) => ({ id: i + 1, text: text(i + 1) }));
}

/** jsdom has no layout: give the output region the geometry of a real scroller. */
function geometry(region: HTMLElement, scrollHeight: number, clientHeight = 400) {
  Object.defineProperty(region, "scrollHeight", { configurable: true, get: () => scrollHeight });
  Object.defineProperty(region, "clientHeight", { configurable: true, get: () => clientHeight });
}

function scrollTo(region: HTMLElement, top: number) {
  region.scrollTop = top;
  fireEvent.scroll(region);
}

beforeEach(() => {
  // The virtualizer sizes its window from offsetHeight, which jsdom reports as 0.
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(400);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(800);
});

describe("LogViewer", () => {
  it("renders ANSI red as a span with the fail colour", () => {
    render(<LogViewer lines={[{ id: 1, text: `${ESC}[31mError:${ESC}[0m health check failed` }]} />);
    const error = screen.getByText("Error:");
    expect(error.tagName).toBe("SPAN");
    expect(error).toHaveClass("text-fail");
    expect(screen.getByText("health check failed")).not.toHaveClass("text-fail");
  });

  it("marks error and warning lines with their state ground", () => {
    render(
      <LogViewer
        lines={[
          { id: 1, text: "deprecated glob@7", level: "warn" },
          { id: 2, text: "502 Bad Gateway", level: "error" },
        ]}
      />,
    );
    expect(screen.getByText("deprecated glob@7").closest("[data-index]")).toHaveClass("bg-warn-soft");
    expect(screen.getByText("502 Bad Gateway").closest("[data-index]")).toHaveClass("bg-fail-soft");
  });

  it("is a named, focusable region that never announces its lines", () => {
    render(<LogViewer lines={lines(3)} label="Build log for example.com" />);
    const region = screen.getByRole("region", { name: "Build log for example.com" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).not.toHaveAttribute("aria-live");
    expect(region.closest("[aria-live]")).toBeNull();
  });

  it("pauses following when the reader scrolls up and offers the way back", async () => {
    const { rerender } = render(<LogViewer lines={lines(200)} label="Journal" />);
    const region = screen.getByRole("region", { name: "Journal" });
    geometry(region, 4000);
    expect(screen.getByRole("button", { name: "Follow" })).toHaveAttribute("aria-pressed", "true");

    scrollTo(region, 3600);
    expect(screen.queryByRole("button", { name: /Jump to latest/ })).not.toBeInTheDocument();

    scrollTo(region, 1200);
    expect(screen.getByRole("button", { name: "Follow" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: /Jump to latest/ })).toBeInTheDocument();

    rerender(<LogViewer lines={lines(205)} label="Journal" />);
    expect(screen.getByRole("button", { name: /Jump to latest/ })).toHaveTextContent("+5");
    expect(region.scrollTop).toBe(1200);

    await userEvent.click(screen.getByRole("button", { name: /Jump to latest/ }));
    expect(screen.queryByRole("button", { name: /Jump to latest/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Follow" })).toHaveAttribute("aria-pressed", "true");
    expect(region.scrollTop).toBe(4000);
  });

  it("resumes following when the reader scrolls back to the bottom", () => {
    render(<LogViewer lines={lines(200)} label="Journal" />);
    const region = screen.getByRole("region", { name: "Journal" });
    geometry(region, 4000);
    scrollTo(region, 3600);
    scrollTo(region, 1000);
    expect(screen.getByRole("button", { name: /Jump to latest/ })).toBeInTheDocument();
    scrollTo(region, 3600);
    expect(screen.queryByRole("button", { name: /Jump to latest/ })).not.toBeInTheDocument();
  });

  it("searches, counts and highlights matches and steps through them", async () => {
    render(
      <LogViewer
        lines={[
          { id: 1, text: "$ npm ci" },
          { id: 2, text: `${ESC}[33mnpm warn${ESC}[0m deprecated inflight` },
          { id: 3, text: "added 812 packages" },
          { id: 4, text: "$ npm run build" },
        ]}
      />,
    );
    const search = screen.getByRole("searchbox", { name: "Search output" });
    await userEvent.type(search, "NPM");
    const status = screen.getByText("1 of 3");
    expect(status).toHaveAttribute("role", "status");
    const region = screen.getByRole("region", { name: "Log output" });
    const marks = region.querySelectorAll("mark");
    expect(marks).toHaveLength(3);
    expect(marks[0]).toHaveAttribute("data-current");

    await userEvent.keyboard("{Enter}");
    expect(status).toHaveTextContent("2 of 3");
    expect(region.querySelectorAll("mark")[1]).toHaveAttribute("data-current");

    await userEvent.click(screen.getByRole("button", { name: "Next match" }));
    await userEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(status).toHaveTextContent("1 of 3");

    search.focus();
    await userEvent.keyboard("{Shift>}{Enter}{/Shift}");
    expect(status).toHaveTextContent("3 of 3");

    await userEvent.clear(search);
    await userEvent.type(search, "zzz");
    expect(status).toHaveTextContent("No matches");

    await userEvent.keyboard("{Escape}");
    expect(search).toHaveValue("");
    expect(region.querySelectorAll("mark")).toHaveLength(0);
  });

  it("jumps to the first match as the reader types, and stops following", async () => {
    render(<LogViewer lines={lines(300, (i) => (i === 7 ? "needle here" : `line ${String(i)}`))} label="Journal" />);
    expect(screen.getByRole("button", { name: "Follow" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.type(screen.getByRole("searchbox", { name: "Search output" }), "needle");
    expect(screen.getByRole("button", { name: "Follow" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("needle").tagName).toBe("MARK");
  });

  it("keeps a highlighted match inside its coloured run", async () => {
    render(<LogViewer lines={[{ id: 1, text: `${ESC}[31mError: timeout${ESC}[0m` }]} />);
    await userEvent.type(screen.getByRole("searchbox", { name: "Search output" }), "time");
    const mark = screen.getByText("time");
    expect(mark.tagName).toBe("MARK");
    expect(screen.getByText("Error:", { exact: false })).toHaveClass("text-fail");
  });

  it("toggles line wrapping", async () => {
    render(<LogViewer lines={lines(2)} />);
    const wrap = screen.getByRole("button", { name: "Wrap lines" });
    expect(wrap).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(wrap);
    expect(wrap).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("line 1")).toHaveClass("whitespace-pre-wrap");
  });

  it("copies the output as plain text, without escape codes", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    vi.stubGlobal("isSecureContext", true);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<LogViewer lines={[{ id: 1, text: `${ESC}[32mok${ESC}[0m` }, { id: 2, text: "done" }]} />);
    await userEvent.click(screen.getByRole("button", { name: "Copy output" }));
    expect(writeText).toHaveBeenCalledWith("ok\ndone");
    vi.unstubAllGlobals();
  });

  it("downloads the output under the given file name", async () => {
    const createObjectURL = vi.fn(() => "blob:log");
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    render(<LogViewer lines={lines(2)} filename="shop-a1b2c3d.log" />);
    await userEvent.click(screen.getByRole("button", { name: "Download output" }));
    expect(createObjectURL).toHaveBeenCalledOnce();
    const anchor = click.mock.contexts[0] as HTMLAnchorElement;
    expect(anchor.download).toBe("shop-a1b2c3d.log");
  });

  it("asks for older lines when scrolled to the top, once per batch", () => {
    const onLoadMore = vi.fn();
    render(<LogViewer lines={lines(100)} follow={false} onLoadMore={onLoadMore} label="Journal" />);
    const region = screen.getByRole("region", { name: "Journal" });
    geometry(region, 2000);
    scrollTo(region, 500);
    scrollTo(region, 10);
    scrollTo(region, 5);
    expect(onLoadMore).toHaveBeenCalledOnce();
  });

  it("says when there is nothing yet", () => {
    render(<LogViewer lines={[]} emptyMessage="Waiting for the build to start." />);
    expect(screen.getByText("Waiting for the build to start.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download output" })).toBeDisabled();
  });

  it("has no accessibility violations, with and without a search", async () => {
    const { container } = render(
      <LogViewer
        lines={[
          { id: 1, text: `${ESC}[1m${ESC}[36m==>${ESC}[0m Installing`, ts: "14:31:01" },
          { id: 2, text: "npm warn deprecated", level: "warn" },
          { id: 3, text: `${ESC}[31mError:${ESC}[0m exit 1`, level: "error" },
        ]}
        label="Build log"
      />,
    );
    await expectNoAxeViolations(container);
    await userEvent.type(within(container).getByRole("searchbox", { name: "Search output" }), "e");
    await expectNoAxeViolations(container);
  });
});
