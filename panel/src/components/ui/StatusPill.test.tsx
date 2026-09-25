import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import type { Status } from "./StatusPill";
import { STATUS, StatusPill } from "./StatusPill";

const STATES = Object.keys(STATUS) as Status[];

describe("StatusPill", () => {
  it("names the state for assistive technology", () => {
    render(<StatusPill state="failed" />);
    expect(screen.getByText("Failed")).toBeVisible();
  });

  it.each(STATES)("says %s in words", (state) => {
    render(<StatusPill state={state} />);
    expect(screen.getByText(STATUS[state].label)).toBeInTheDocument();
  });

  it("gives every state its own shape, so colour is never the only signal", () => {
    const { container } = render(
      <div>
        {STATES.map((state) => (
          <StatusPill key={state} state={state} />
        ))}
      </div>,
    );
    const glyphs = [...container.querySelectorAll("svg[data-glyph]")].map((svg) => svg.getAttribute("data-glyph"));
    expect(new Set(glyphs).size).toBe(STATES.length);
    for (const svg of container.querySelectorAll("svg")) expect(svg).toHaveAttribute("aria-hidden", "true");
  });

  it("uses a colour per state tone", () => {
    render(<StatusPill state="running" />);
    expect(screen.getByText("Running")).toHaveClass("text-ok");
  });

  it("accepts a more specific word", () => {
    render(<StatusPill state="deploying" label="Building" />);
    expect(screen.getByText("Building")).toBeInTheDocument();
    expect(screen.queryByText("Deploying")).not.toBeInTheDocument();
  });

  it("pulses once when the state changes, not on first render", () => {
    const { rerender } = render(<StatusPill state="deploying" />);
    expect(screen.getByText("Deploying")).not.toHaveClass("animate-pulse-once");
    rerender(<StatusPill state="running" />);
    expect(screen.getByText("Running")).toHaveClass("animate-pulse-once");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        {STATES.map((state) => (
          <StatusPill key={state} state={state} />
        ))}
        {STATES.map((state) => (
          <StatusPill key={`inline-${state}`} state={state} appearance="inline" size="sm" />
        ))}
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});
