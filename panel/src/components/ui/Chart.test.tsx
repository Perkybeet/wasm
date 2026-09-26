import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import type { ChartMarker, MarkerPlot } from "./Chart";
import { Chart, continuing, formatChartTime, markersInRange, needsDateFormat, positionMarkers, valueAxisSize } from "./Chart";

// uPlot draws on a canvas, which jsdom does not implement; the wrapper's contract is the
// accessible summary, the table, the marker overlay and the lifecycle of the plot, all
// testable without pixels. The fake plot below implements just enough of uPlot's own
// geometry (`bbox`, `valToPos`) for the chart's draw hook to position markers for real.
const plots = vi.hoisted(
  () => [] as { options: Record<string, unknown>; data: unknown; destroy: () => void; setData: (d: unknown) => void }[],
);

vi.mock("uplot", () => {
  const bbox = { left: 40, top: 8, width: 400, height: 144 };

  function build(options: Record<string, unknown>, initialData: unknown) {
    let current = initialData as number[][];
    const draws = ((options["hooks"] as { draw?: ((u: unknown) => void)[] } | undefined)?.draw ?? []) as ((
      u: unknown,
    ) => void)[];
    const valToPos = (val: number): number => {
      const xs = current[0] ?? [];
      const min = xs[0] ?? 0;
      const max = xs.at(-1) ?? min + 1;
      const frac = max > min ? (val - min) / (max - min) : 0;
      return bbox.left + frac * bbox.width;
    };
    const ctx = {
      save: vi.fn(),
      restore: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      stroke: vi.fn(),
      setLineDash: vi.fn(),
    };
    const plot = {
      options,
      data: current,
      bbox,
      ctx,
      valToPos: (val: number) => valToPos(val),
      destroy: vi.fn(),
      setSize: vi.fn(),
      redraw: vi.fn(),
      setData: vi.fn((next: unknown) => {
        current = next as number[][];
        plot.data = current;
        draws.forEach((fn) => {
          fn(plot);
        });
      }),
    };
    plots.push(plot);
    draws.forEach((fn) => {
      fn(plot);
    });
    return plot;
  }

  const ctor = vi.fn(function (this: unknown, options: Record<string, unknown>, data: unknown) {
    return build(options, data);
  }) as unknown as { new (...args: unknown[]): unknown; pxRatio: number };
  ctor.pxRatio = 1;
  return { default: ctor };
});

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

describe("Chart markers", () => {
  const inRange: ChartMarker = {
    at: T0 + 60,
    label: "Deploy 25, succeeded, 14:01",
    state: "running",
    href: "https://wasm.example.com/deploys/25",
  };
  const outOfRange: ChartMarker = {
    at: T0 - 600,
    label: "Deploy 9, failed, 13:50",
    state: "failed",
    href: "https://wasm.example.com/deploys/9",
  };

  function WithMarkers({ markers }: { markers: readonly ChartMarker[] }) {
    return (
      <Chart
        title="CPU"
        timestamps={TIMES}
        series={[{ label: "shop.example.com", values: [12, 48, 30] }]}
        formatValue={(v) => `${String(v)}%`}
        markers={markers}
      />
    );
  }

  it("draws an in-range marker as a focusable link with its full accessible name", () => {
    render(<WithMarkers markers={[inRange]} />);
    const link = screen.getByRole("link", { name: inRange.label });
    expect(link).toHaveAttribute("href", "https://wasm.example.com/deploys/25");
  });

  it("does not draw a marker outside the time range", () => {
    render(<WithMarkers markers={[outOfRange]} />);
    expect(screen.queryByRole("link", { name: outOfRange.label })).not.toBeInTheDocument();
  });

  it("mentions how many markers are in view in the accessible summary", () => {
    render(<WithMarkers markers={[inRange, outOfRange]} />);
    expect(screen.getByRole("img")).toHaveAccessibleName(/1 marker in view\.$/);
  });

  it("says zero markers when none of them fall in the range", () => {
    render(<WithMarkers markers={[outOfRange]} />);
    expect(screen.getByRole("img")).toHaveAccessibleName(/0 markers in view\.$/);
  });

  it("says nothing about markers when the prop is not used", () => {
    render(<Example />);
    expect(screen.getByRole("img")).not.toHaveAccessibleName(/marker/);
  });

  it("uses renderMarker to wrap the affordance, keeping Chart free of a router", () => {
    const marker: ChartMarker = {
      at: T0 + 60,
      label: "Deploy 25, succeeded, 14:01",
      state: "running",
      renderMarker: (m, children, linkProps) => (
        <button type="button" data-testid="custom-marker" aria-label={m.label} className={linkProps.className} style={linkProps.style}>
          {children}
        </button>
      ),
    };
    render(<WithMarkers markers={[marker]} />);
    expect(screen.getByTestId("custom-marker")).toHaveAccessibleName(marker.label);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("falls back to a real, natively focusable control with neither href nor renderMarker", () => {
    const marker: ChartMarker = { at: T0 + 60, label: "Release 9, unlinked", state: "stopped" };
    render(<WithMarkers markers={[marker]} />);
    expect(screen.getByRole("button", { name: "Release 9, unlinked" })).toBeInTheDocument();
  });

  it("falls back to an aria-disabled control rather than a link for an href that is not http(s)", () => {
    const marker: ChartMarker = {
      at: T0 + 60,
      label: "Deploy 25, succeeded, 14:01",
      state: "running",
      href: "javascript:alert(1)",
    };
    render(<WithMarkers markers={[marker]} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    const button = screen.getByRole("button", { name: marker.label });
    expect(button).toHaveAttribute("aria-disabled", "true");
  });

  it("keeps markers in the table view so they do not vanish for screen reader users", async () => {
    render(<WithMarkers markers={[inRange, outOfRange]} />);
    await userEvent.click(screen.getByRole("button", { name: "View as table" }));
    // Listed below the table itself (not inside its scrolling region, so a sighted user
    // never has to scroll a small box to find them) but still reachable in the document.
    expect(screen.getByRole("region", { name: "CPU data" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: inRange.label })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: outOfRange.label })).not.toBeInTheDocument();
  });

  it("has no accessibility violations with markers, as a chart or as a table", async () => {
    const { container } = render(<WithMarkers markers={[inRange, outOfRange]} />);
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

describe("needsDateFormat", () => {
  const DAY = 86_400;

  it("stays a clock under about two days", () => {
    expect(needsDateFormat([T0, T0 + 3_600, T0 + 2 * DAY - 60])).toBe(false);
  });

  it("wants a date once the span passes about two days", () => {
    expect(needsDateFormat([T0, T0 + 7 * DAY])).toBe(true);
  });

  it("wants a date for a short span that crosses local midnight", () => {
    const before = new Date(2026, 8, 25, 23, 30).getTime() / 1000;
    const after = new Date(2026, 8, 26, 0, 30).getTime() / 1000;
    expect(needsDateFormat([before, after])).toBe(true);
  });

  it("stays a clock with fewer than two points", () => {
    expect(needsDateFormat([T0])).toBe(false);
    expect(needsDateFormat([])).toBe(false);
  });
});

describe("formatChartTime", () => {
  // Built from local Date fields, like formatChartTime itself: the string it produces is
  // then independent of the machine's own time zone.
  const local = new Date(2026, 8, 25, 14, 0).getTime() / 1000;

  it("prints a bare 24-hour clock without a date", () => {
    expect(formatChartTime(local, false)).toBe("14:00");
  });

  it("prints the date and the clock with a date", () => {
    expect(formatChartTime(local, true)).toBe("Sep 25 14:00");
  });

  it("prints the bare date at a tick that lands exactly on local midnight", () => {
    const midnight = new Date(2026, 8, 25, 0, 0).getTime() / 1000;
    expect(formatChartTime(midnight, true)).toBe("Sep 25");
  });
});

describe("markersInRange", () => {
  const markers: ChartMarker[] = [
    { at: 100, label: "a", state: "running" },
    { at: 200, label: "b", state: "running" },
    { at: 300, label: "c", state: "running" },
  ];

  it("keeps markers within the bounds, inclusive", () => {
    expect(markersInRange(markers, 100, 200).map((m) => m.label)).toEqual(["a", "b"]);
  });

  it("drops everything when the range is empty or inverted", () => {
    expect(markersInRange(markers, 500, 600)).toEqual([]);
    expect(markersInRange(markers, 300, 100)).toEqual([]);
  });
});

describe("positionMarkers", () => {
  // canvasPixels doubles the value (a stand-in for a pxRatio of 2); CSS pixels pass it through.
  const plot: MarkerPlot = {
    bbox: { left: 80, top: 20, width: 200, height: 100 },
    valToPos: (value, _scale, canvasPixels) => (canvasPixels ? value * 2 : value),
  };

  it("places each marker at its x, offset by the plot area's own left and top", () => {
    const markers: ChartMarker[] = [{ at: 50, label: "mid", state: "running" }];
    const [position] = positionMarkers(plot, markers, 2);
    // bbox is in canvas pixels; a pxRatio of 2 halves it to the CSS-pixel offset (40, 10).
    expect(position).toEqual({ marker: markers[0], left: 40 + 50, top: 10 });
  });

  it("returns one position per marker, in order", () => {
    const markers: ChartMarker[] = [
      { at: 0, label: "start", state: "running" },
      { at: 100, label: "end", state: "running" },
    ];
    expect(positionMarkers(plot, markers, 2).map((p) => p.marker.label)).toEqual(["start", "end"]);
  });
});

describe("continuing", () => {
  it("reads a description on after the title without shouting or doubling the full stop", () => {
    expect(continuing("Last 24 hours. Percent of one CPU.")).toBe("last 24 hours. Percent of one CPU");
    expect(continuing("Last hour, of 20 GB")).toBe("last hour, of 20 GB");
    expect(continuing("CPU of the unit")).toBe("CPU of the unit");
  });
});
