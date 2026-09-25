import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { SegmentedControl } from "./SegmentedControl";

const WINDOWS = [
  { value: "1h", label: "1h" },
  { value: "24h", label: "24h" },
  { value: "30d", label: "30d" },
] as const;

type Window = (typeof WINDOWS)[number]["value"];

function Picker({ onChange }: { onChange?: (value: Window) => void }) {
  const [value, setValue] = useState<Window>("1h");
  return (
    <SegmentedControl
      label="Time range"
      options={WINDOWS}
      value={value}
      onValueChange={(next) => {
        setValue(next);
        onChange?.(next);
      }}
    />
  );
}

describe("SegmentedControl", () => {
  it("is a named radio group with the current choice checked", () => {
    render(<Picker />);
    expect(screen.getByRole("radiogroup", { name: "Time range" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "1h" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "24h" })).toHaveAttribute("aria-checked", "false");
  });

  it("changes on click and with the arrow keys", async () => {
    const onChange = vi.fn();
    render(<Picker onChange={onChange} />);
    await userEvent.click(screen.getByRole("radio", { name: "24h" }));
    expect(onChange).toHaveBeenLastCalledWith("24h");
    await userEvent.keyboard("{ArrowRight}");
    expect(onChange).toHaveBeenLastCalledWith("30d");
    expect(screen.getByRole("radio", { name: "30d" })).toHaveAttribute("aria-checked", "true");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(<Picker />);
    await expectNoAxeViolations(container);
  });
});
