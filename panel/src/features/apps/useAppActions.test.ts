import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, ElevationCancelledError } from "../../api/errors";
import { toast } from "../../components/ui/toast";
import { reportActionError } from "./useAppActions";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("reportActionError", () => {
  it("shows the fix and the system's words, apart", () => {
    const error = vi.spyOn(toast, "error");
    reportActionError("Restart of shop.example.com failed", new ApiError(500, "internal", "systemctl restart failed", "Check the logs."));
    expect(error).toHaveBeenCalledWith("Restart of shop.example.com failed", {
      detail: "systemctl restart failed",
      description: "Check the logs.",
    });
  });

  it("says a cancelled confirmation is not a failure, instead of reporting an error", () => {
    const info = vi.spyOn(toast, "info");
    const error = vi.spyOn(toast, "error");
    reportActionError("Delete failed", new ElevationCancelledError());
    expect(info).toHaveBeenCalledWith("Nothing was changed because the confirmation was cancelled.");
    expect(error).not.toHaveBeenCalled();
  });

  it("carries a failing tool's own output (git's, psql's, certbot's) apart from the one-line detail", () => {
    const error = vi.spyOn(toast, "error");
    const apiError = new ApiError(
      400,
      "source_error",
      "Could not access git@github.com:acme/private.git",
      null,
      null,
      null,
      "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.",
    );
    reportActionError("Could not renew shop.example.com", apiError);
    expect(error).toHaveBeenCalledWith("Could not renew shop.example.com", {
      detail: "Could not access git@github.com:acme/private.git",
      output: "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.",
    });
  });

  it("passes output through even when it reads the same as detail: deduplicating it is the toast queue's job", () => {
    const error = vi.spyOn(toast, "error");
    reportActionError("Failed", new ApiError(400, "rejected", "Permission denied", null, null, null, "Permission denied"));
    expect(error).toHaveBeenCalledWith("Failed", { detail: "Permission denied", output: "Permission denied" });
  });
});
