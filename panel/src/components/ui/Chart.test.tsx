import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Chart, valueAxisSize } from "./Chart";

// uPlot draws on a canvas, which jsdom does not implement; the wrapper's contract is the
// accessible summary, the table and the lifecycle of the plot, all testable without pixels.
const plots = vi.hoisted(() => [] as { options: unknown; data: unknown; destroy: () => void; setData: (d: unknown) => void }[]);

vi.mock("uplot", () => ({
  default: vi.fn(function (this: Record<string, unknown>, options: unknown, data: unknown) {
    const plot = { options, data, destroy: vi.fn(), setData: vi.fn(), setSize: vi.fn(), redraw: vi.fn() };
    plots.push(plot);
    return plot;
  }),
}));

const T0 = Date.UTC(2026, 8, 25, 14, 0) / 1000;
const TIMES = [T0, T0 + 60, T0 + 120];

beforeEach(() => {
  plots.length = 0;
});

function Example() {
  return (
    <Chart
      title="CPU"
      description="Last 3 minutes"
      timestamps={TIMES}
      series={[{ label: "shop.example.com", values: [12, 48, 30] }]}
      formatValue={(v) => `${String(v)}%`}
      yRange={[0, 100]}
    />
  );
}

describe("Chart", () => {
  it("is an image with a written summary of every series", () => {
    render(<Example />);
    const image = screen.getByRole("img");
    expect(image).toHaveAccessibleName(
      "CPU, last 3 minutes. shop.example.com: latest 30%, low 12%, high 48%.",
    );
  });

  it("draws the data with uPlot and destroys the plot when it goes", () => {
    const { unmount } = render(<Example />);
    expect(plots.length).toBeGreaterThan(0);
    const plot = plots.at(-1);
    expect(plot?.data).toEqual([TIMES, [12, 48, 30]]);
    unmount();
    expect(plot?.destroy).toHaveBeenCalled();
  });

  it("shows the latest value of each series in the legend", () => {
    render(<Example />);
    expect(within(screen.getByRole("list", { name: "Series" })).getByText("30%")).toBeInTheDocument();
  });

  it("turns into a table of the same numbers, newest first", async () => {
    render(<Example />);
    const toggle = screen.getByRole("button", { name: "View as table" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    const table = screen.getByRole("table", { name: "CPU, newest first" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell")[1]?.textContent)).toEqual(["30%", "48%", "12%"]);
    await userEvent.click(toggle);
    expect(screen.getByRole("img")).toBeInTheDocument();
  });

  it("has no accessibility violations as a chart or as a table", async () => {
    const { container } = render(<Example />);
    await expectNoAxeViolations(container);
    await userEvent.click(screen.getByRole("button", { name: "View as table" }));
    await expectNoAxeViolations(container);
  });
});

describe("valueAxisSize", () => {
  // A canvas that measures 6.6px per character, like 11px JetBrains Mono.
  const ctx = { font: "", measureText: (text: string) => ({ width: text.length * 6.6 }) } as unknown as CanvasRenderingContext2D;

  it("fits the widest label, so a long unit is never clipped", () => {
    expect(valueAxisSize({ ctx }, ["0 B/s", "386 KB/s", "771 KB/s"])).toBe(Math.ceil(8 * 6.6 + 12));
    expect(ctx.font).toContain("JetBrains Mono");
  });

  it("keeps a minimum width for short labels and before the first draw", () => {
    expect(valueAxisSize({ ctx }, ["0%", "50%"])).toBe(40);
    expect(valueAxisSize({ ctx }, null)).toBe(40);
  });
});
