import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../app/App";
import { fakeBackend, json, signedInRoutes } from "../../test/fakes";
import { EnrollDialog } from "./TwoFactorSection";

const ENROLLMENT = {
  secret: "JBSWY3DPEHPK3PXP",
  uri: "otpauth://totp/WASM:web-01?secret=JBSWY3DPEHPK3PXP&issuer=WASM&algorithm=SHA1&digits=6&period=30",
};

const CODES = ["1a2b-3c4d", "5e6f-7a8b"];

function renderDialog(onClose: () => void) {
  const client = createQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <EnrollDialog open enrollment={ENROLLMENT} onClose={onClose} />
    </QueryClientProvider>,
  );
}

describe("TwoFactorSection > EnrollDialog", () => {
  it("clears the typed code and the backup codes once it closes, even without the parent remounting it", async () => {
    fakeBackend({
      ...signedInRoutes(),
      "POST /api/auth/2fa/confirm": () => json(200, { success: true, backup_codes: CODES }),
    });
    const onClose = vi.fn();
    renderDialog(onClose);
    const user = userEvent.setup();

    const dialog = await screen.findByRole("dialog", { name: "Set up two-factor authentication" });
    await user.type(within(dialog).getByLabelText("Authentication code"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Turn on" }));

    const codesDialog = await screen.findByRole("dialog", { name: "Save your backup codes" });
    expect(within(codesDialog).getAllByRole("listitem").map((item) => item.textContent)).toEqual(CODES);
    await user.click(within(codesDialog).getByRole("checkbox", { name: "I have saved these codes somewhere safe" }));
    await user.click(within(codesDialog).getByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalledOnce();

    // The dialog itself never unmounted (its `open` prop never changed in this test): if
    // closing did not wipe its own state, the backup codes and the typed code would still be
    // sitting there. Instead this shows the QR step fresh, exactly as a brand new setup would.
    expect(await screen.findByRole("dialog", { name: "Set up two-factor authentication" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Save your backup codes" })).not.toBeInTheDocument();
    expect(screen.queryByText(CODES[0] ?? "")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Authentication code")).toHaveValue("");
  });

  it("clears the typed code when cancelled before confirming", async () => {
    fakeBackend(signedInRoutes());
    const onClose = vi.fn();
    renderDialog(onClose);
    const user = userEvent.setup();

    const dialog = await screen.findByRole("dialog", { name: "Set up two-factor authentication" });
    await user.type(within(dialog).getByLabelText("Authentication code"), "000000");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledOnce();

    expect(await screen.findByLabelText("Authentication code")).toHaveValue("");
  });
});
