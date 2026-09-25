import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Meter, Progress } from "./Progress";

describe("Progress", () => {
  it("reports how far a task has got", () => {
    render(<Progress label="Uploading backup" value={64} />);
    const bar = screen.getByRole("progressbar", { name: "Uploading backup" });
    expect(bar).toHaveAttribute("aria-valuenow", "64");
    expect(screen.getByText("64%")).toBeInTheDocument();
  });

  it("has no value while the total is unknown", () => {
    render(<Progress label="Waiting for the build slot" value={null} />);
    expect(screen.getByRole("progressbar", { name: "Waiting for the build slot" })).not.toHaveAttribute("aria-valuenow");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        <Progress label="Uploading backup" value={64} />
        <Progress label="Waiting" value={null} />
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});

describe("Meter", () => {
  it("reads a level with its own words", () => {
    render(<Meter label="Memory" value={6.2} max={8} valueText="6.2 of 8 GB" />);
    const meter = screen.getByRole("meter", { name: "Memory" });
    expect(meter).toHaveAttribute("aria-valuetext", "6.2 of 8 GB");
    expect(screen.getByText("6.2 of 8 GB")).toBeInTheDocument();
  });

  it("turns amber, then red, as the level becomes a problem", () => {
    const { container } = render(
      <div>
        <Meter label="CPU" value={23} />
        <Meter label="Memory" value={80} />
        <Meter label="Disk" value={95} />
      </div>,
    );
    const levels = [...container.querySelectorAll("[data-level]")].map((node) => node.getAttribute("data-level"));
    expect(levels).toEqual(["normal", "warn", "fail"]);
  });

  it("defaults to a percentage", () => {
    render(<Meter label="CPU" value={23} />);
    expect(screen.getByRole("meter", { name: "CPU" })).toHaveAttribute("aria-valuetext", "23%");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        <Meter label="CPU" value={23} />
        <Meter label="Disk" value={71} max={75} valueText="71 of 75 GB" size="sm" />
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});
