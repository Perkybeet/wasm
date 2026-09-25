import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/errors";
import { splitErrors } from "./formErrors";

describe("splitErrors", () => {
  it("puts a 422's messages beside the fields they name, verbatim", () => {
    const error = new ApiError(422, "validation_error", "Validation failed", null, {
      max_per_app: "Input should be less than or equal to 100",
    });
    expect(splitErrors(error, ["directory", "max_per_app"])).toEqual({
      fields: { max_per_app: "Input should be less than or equal to 100" },
      form: null,
    });
  });

  it("keeps the form-level error when the server also names a field this form does not show", () => {
    const error = new ApiError(422, "validation_error", "Validation failed", null, { port: "bad", session_timeout: "bad" });
    const split = splitErrors(error, ["host", "port"]);
    expect(split.fields).toEqual({ port: "bad" });
    expect(split.form).toBe(error);
  });

  it("gives a one-field form a refusal that names no field, with the server's fix", () => {
    const error = new ApiError(
      400,
      "configerror",
      "apps_directory must be an absolute path",
      "Got 'relative/apps'. Use a path starting with '/', such as /var/www/apps.",
    );
    expect(splitErrors(error, ["apps_directory"], "apps_directory")).toEqual({
      fields: {
        apps_directory:
          "apps_directory must be an absolute path Got 'relative/apps'. Use a path starting with '/', such as /var/www/apps.",
      },
      form: null,
    });
  });

  it("leaves failures that are not about a value above the form", () => {
    const denied = new ApiError(403, "forbidden", "Permission denied writing to /etc/wasm/config.yaml");
    expect(splitErrors(denied, ["apps_directory"], "apps_directory")).toEqual({ fields: {}, form: denied });
    const down = new ApiError(0, "network", "Failed to fetch");
    expect(splitErrors(down, ["email"], "email").form).toBe(down);
  });

  it("says nothing when nothing failed", () => {
    expect(splitErrors(null, ["email"])).toEqual({ fields: {}, form: null });
  });
});
