import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Field } from "./Field";
import { Select } from "./Select";

const RUNTIMES = [
  { value: "node", label: "Node.js 22", hint: "npm ci && npm run build" },
  { value: "python", label: "Python 3.12" },
  { value: "static", label: "Static site", disabled: true },
];

describe("Select", () => {
  it("shows the placeholder until something is chosen", () => {
    render(<Select aria-label="Runtime" options={RUNTIMES} placeholder="Choose a runtime" />);
    expect(screen.getByRole("combobox", { name: "Runtime" })).toHaveTextContent("Choose a runtime");
  });

  it("opens the list and reports the chosen value", async () => {
    const onValueChange = vi.fn();
    render(<Select label="Runtime" options={RUNTIMES} onValueChange={onValueChange} />);
    await userEvent.click(screen.getByRole("combobox", { name: "Runtime" }));
    const option = await screen.findByRole("option", { name: /Python 3.12/ });
    await userEvent.click(option);
    expect(onValueChange).toHaveBeenCalledWith("python");
    expect(screen.getByRole("combobox", { name: "Runtime" })).toHaveTextContent("Python 3.12");
  });

  it("shows the second line of an option and keeps disabled options unselectable", async () => {
    render(<Select aria-label="Runtime" options={RUNTIMES} />);
    await userEvent.click(screen.getByRole("combobox", { name: "Runtime" }));
    expect(await screen.findByText("npm ci && npm run build")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Static site/ })).toHaveAttribute("aria-disabled", "true");
  });

  it("is labelled by an enclosing Field", () => {
    render(
      <Field label="Runtime" nativeLabel={false}>
        <Select options={RUNTIMES} defaultValue="node" />
      </Field>,
    );
    expect(screen.getByRole("combobox", { name: "Runtime" })).toHaveTextContent("Node.js 22");
  });

  it("has no accessibility violations, closed or open", async () => {
    const { container } = render(<Select label="Runtime" options={RUNTIMES} defaultValue="node" />);
    await expectNoAxeViolations(container);
    await userEvent.click(screen.getByRole("combobox", { name: "Runtime" }));
    await screen.findByRole("listbox");
    await expectNoAxeViolations(document.body);
  });
});
