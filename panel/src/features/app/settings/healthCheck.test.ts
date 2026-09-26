import { describe, expect, it } from "vitest";

import { effectiveHealth, healthFieldOf, parseHealth, parseRetention } from "./healthCheck";

describe("parseHealth", () => {
  it("sends an empty field as null: the default", () => {
    expect(parseHealth({ path: " ", expect: "", timeout: "" })).toEqual({ values: { path: null, expect: null, timeout: null }, errors: {} });
  });

  it.each(["/", "/healthz", "/api/health?deep=1", "/a%20b"])("accepts the path %s", (path) => {
    expect(parseHealth({ path, expect: "", timeout: "" }).errors).toEqual({});
  });

  it.each(["healthz", "//evil.example.com/", "https://example.com/", "/with space", "/café"])("refuses the path %s", (path) => {
    expect(parseHealth({ path, expect: "", timeout: "" }).errors.path).toBeDefined();
  });

  it.each(["200", "200-399", "200,204", "200-299, 301"])("accepts the statuses %s", (expect_) => {
    expect(parseHealth({ path: "", expect: expect_, timeout: "" }).errors).toEqual({});
  });

  it.each(["2xx", "600", "399-200", "099", "200,,204", "ok"])("refuses the statuses %s", (expect_) => {
    expect(parseHealth({ path: "", expect: expect_, timeout: "" }).errors.expect).toBeDefined();
  });

  it("holds the timeout to 5 to 600 whole seconds", () => {
    expect(parseHealth({ path: "", expect: "", timeout: "5" }).values.timeout).toBe(5);
    expect(parseHealth({ path: "", expect: "", timeout: "600" }).values.timeout).toBe(600);
    for (const timeout of ["4", "601", "1.5", "-5", "soon"]) {
      expect(parseHealth({ path: "", expect: "", timeout }).errors.timeout).toBeDefined();
    }
  });
});

describe("healthFieldOf", () => {
  it("reads the field off the validator's own sentence", () => {
    expect(healthFieldOf("A health check timeout of 2 seconds is out of range")).toBe("timeout");
    expect(healthFieldOf("'2xx' is not a list of HTTP statuses")).toBe("expect");
    expect(healthFieldOf("'x' is not a path on the application")).toBe("path");
    expect(healthFieldOf("'/a b' contains a space, a control character or a character outside ASCII")).toBe("path");
    expect(healthFieldOf("Application not found: x")).toBeNull();
  });
});

describe("effectiveHealth", () => {
  it("fills the defaults in, and says which they are", () => {
    expect(effectiveHealth({ health_path: "/up", health_expect: null, health_timeout: null })).toEqual({
      path: "/up",
      expect: "any status below 500",
      timeout: 30,
      defaults: { path: false, expect: true, timeout: true },
    });
  });
});

describe("parseRetention", () => {
  it("keeps from 1 to 50 releases", () => {
    expect(parseRetention("1")).toEqual({ keep: 1, error: null });
    expect(parseRetention(" 50 ")).toEqual({ keep: 50, error: null });
    expect(parseRetention("0").error).toBe("Keep from 1 to 50 releases.");
    expect(parseRetention("51").keep).toBeNull();
    expect(parseRetention("").error).toMatch(/whole number/);
    expect(parseRetention("3.5").error).toMatch(/whole number/);
  });
});
