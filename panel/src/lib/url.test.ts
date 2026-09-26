import { describe, expect, it } from "vitest";

import { isHttpUrl } from "./url";

describe("isHttpUrl", () => {
  it("accepts absolute http and https URLs", () => {
    expect(isHttpUrl("https://github.com/Perkybeet/wasm")).toBe(true);
    expect(isHttpUrl("http://internal.example.com/releases")).toBe(true);
  });

  it("refuses a scheme built to run script or leave the browser", () => {
    expect(isHttpUrl("javascript:alert(1)")).toBe(false);
    expect(isHttpUrl("data:text/html,<script>alert(1)</script>")).toBe(false);
    expect(isHttpUrl("vbscript:msgbox(1)")).toBe(false);
  });

  it("refuses a relative path, a fragment, or a bare string", () => {
    expect(isHttpUrl("/apps/shop.example.com")).toBe(false);
    expect(isHttpUrl("#deploy-25")).toBe(false);
    expect(isHttpUrl("shop.example.com")).toBe(false);
    expect(isHttpUrl("")).toBe(false);
  });
});
