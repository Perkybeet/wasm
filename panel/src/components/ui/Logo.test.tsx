import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Logo, LogoMark } from "./Logo";

describe("Logo", () => {
  it("names the mark when it stands alone", () => {
    render(<LogoMark title="WASM" />);
    expect(screen.getByRole("img", { name: "WASM" })).toBeInTheDocument();
  });

  it("hides the mark next to the wordmark, which carries the name", () => {
    const { container } = render(<Logo product="Console" />);
    expect(screen.getByText("WASM")).toBeInTheDocument();
    expect(screen.getByText("Console")).toBeInTheDocument();
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("gives each instance its own gradient so several marks can share a page", () => {
    const { container } = render(
      <div>
        <LogoMark />
        <LogoMark />
      </div>,
    );
    const ids = [...container.querySelectorAll("linearGradient")].map((node) => node.id);
    expect(new Set(ids).size).toBe(2);
    for (const id of ids) expect(id).toMatch(/^[a-zA-Z0-9_-]+$/);
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        <LogoMark title="WASM" size={32} />
        <Logo size="lg" />
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});
