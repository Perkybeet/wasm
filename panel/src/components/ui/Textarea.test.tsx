import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Field } from "./Field";
import { Textarea } from "./Textarea";

describe("Textarea", () => {
  it("is labelled by its field and accepts several lines", async () => {
    render(
      <Field label="Environment">
        <Textarea mono />
      </Field>,
    );
    const area = screen.getByRole("textbox", { name: "Environment" });
    await userEvent.type(area, "A=1{Enter}B=2");
    expect(area).toHaveValue("A=1\nB=2");
  });

  it("does not spell-check mono content such as keys and secrets", () => {
    render(
      <Field label="Environment">
        <Textarea mono />
      </Field>,
    );
    expect(screen.getByRole("textbox", { name: "Environment" })).toHaveAttribute("spellcheck", "false");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <Field label="Environment" description="One KEY=value per line." error="Line 3 has no '='.">
        <Textarea mono defaultValue={"A=1\nB=2\nC"} />
      </Field>,
    );
    await expectNoAxeViolations(container);
  });
});
