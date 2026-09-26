import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/errors";
import { splitConfigErrors, splitErrors } from "./formErrors";

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

describe("splitConfigErrors", () => {
  const KEYS = { "monitor.smtp.host": "host", "monitor.smtp.port": "port", "monitor.email_recipients": "recipients" } as const;
  const NAMES = ["host", "port", "recipients"] as const;

  it("puts a refusal worded by its configuration key beside that key's field, with its fix", () => {
    const error = new ApiError(400, "configerror", "monitor.smtp.host is not a valid hostname: bad", "Use a hostname such as smtp.example.com.");
    expect(splitConfigErrors(error, NAMES, KEYS)).toEqual({
      fields: { host: "monitor.smtp.host is not a valid hostname: bad Use a hostname such as smtp.example.com." },
      form: null,
    });
  });

  it("still takes a 422's fields as they are named", () => {
    const error = new ApiError(422, "validation_error", "Validation failed", null, { port: "Input should be a valid integer" });
    expect(splitConfigErrors(error, NAMES, KEYS)).toEqual({ fields: { port: "Input should be a valid integer" }, form: null });
  });

  it("leaves a refusal that names no field of this form above it", () => {
    const other = new ApiError(400, "configerror", "backup.directory must be an absolute path");
    expect(splitConfigErrors(other, NAMES, KEYS)).toEqual({ fields: {}, form: other });
    // A key's name inside a longer one is not that key.
    const longer = new ApiError(400, "configerror", "monitor.smtp.hostname_suffix is odd");
    expect(splitConfigErrors(longer, NAMES, KEYS).form).toBe(longer);
    const server = new ApiError(500, "internal", "monitor.smtp.host exploded");
    expect(splitConfigErrors(server, NAMES, KEYS).form).toBe(server);
    expect(splitConfigErrors(null, NAMES, KEYS)).toEqual({ fields: {}, form: null });
  });
});
