import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { SystemOutput } from "./SystemOutput";

/** jsdom lays nothing out: pretend every box is 100px tall and its content `content` tall. */
function layout(content: number): void {
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(100);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(content);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SystemOutput", () => {
  it("shows the output verbatim and adds no tab stop while it fits", () => {
    layout(80);
    render(<SystemOutput label="What nginx said">{"nginx: [emerg]  two  spaces\n  indented"}</SystemOutput>);
    const output = screen.getByText(/nginx: \[emerg\]/);
    expect(output.tagName).toBe("PRE");
    expect(output.textContent).toBe("nginx: [emerg]  two  spaces\n  indented");
    expect(output).not.toHaveAttribute("tabindex");
    expect(screen.queryByRole("region")).toBeNull();
  });

  it("takes focus, named, once it scrolls, so a keyboard can scroll it", async () => {
    layout(400);
    render(<SystemOutput label="What nginx said">{"a\nb\nc"}</SystemOutput>);
    const region = await screen.findByRole("region", { name: "What nginx said" });
    expect(region).toHaveAttribute("tabindex", "0");
    await expectNoAxeViolations(region.parentElement ?? document.body);
  });

  it("follows its content: new lines can start the scrolling", async () => {
    layout(80);
    const { rerender } = render(<SystemOutput label="Build output">{"one line"}</SystemOutput>);
    expect(screen.queryByRole("region")).toBeNull();
    layout(400);
    rerender(<SystemOutput label="Build output">{"one line\nand many more"}</SystemOutput>);
    await waitFor(() => {
      expect(screen.getByRole("region", { name: "Build output" })).toHaveAttribute("tabindex", "0");
    });
  });
});
