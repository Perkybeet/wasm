import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { AppStatePill, DeployStatePill } from "./AppStatePill";

describe("AppStatePill", () => {
  it("draws the backend's word in the matching state", () => {
    render(<AppStatePill status="No answer" />);
    const pill = screen.getByText("No answer");
    expect(pill).toHaveAttribute("data-state", "failed");
    expect(pill).toHaveClass("text-fail");
  });

  it("pulses once when the state changes, like every StatusPill", () => {
    const { rerender } = render(<AppStatePill status="running" />);
    rerender(<AppStatePill status="deploying" />);
    expect(screen.getByText("Deploying")).toHaveClass("animate-pulse-once");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        {["running", "deploying", "failed", "stopped", "static", "Restarting", "No answer", "mystery"].map((status) => (
          <AppStatePill key={status} status={status} />
        ))}
        <AppStatePill status="running" appearance="inline" size="sm" />
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});

describe("DeployStatePill", () => {
  it("names a deployment's outcome", () => {
    render(<DeployStatePill status="rolled_back" />);
    expect(screen.getByText("Rolled back")).toHaveAttribute("data-state", "stopped");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        {["queued", "running", "success", "failed", "rolled_back"].map((status) => (
          <DeployStatePill key={status} status={status} />
        ))}
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});
