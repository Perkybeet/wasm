import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";

function aboutRoutes(version: Record<string, unknown>): Record<string, RouteHandler> {
  return {
    ...signedInRoutes(),
    "GET /api/system/version": () => json(200, version),
    "GET /api/config": () => json(200, { config: {}, path: "/etc/wasm/config.yaml", writable: true }),
  };
}

describe("Settings > About", () => {
  it("offers an update with the command that installs it, and passes axe", { timeout: 20_000 }, async () => {
    fakeBackend(
      aboutRoutes({
        current_version: "2.0.0",
        latest_version: "2.1.0",
        has_update: true,
        update_command: "pip install --upgrade wasm-cli",
        release_url: "https://github.com/Perkybeet/wasm/releases/tag/v2.1.0",
      }),
    );
    const { container } = renderConsole("/settings/about");
    const version = await screen.findByRole("region", { name: "Version and updates" });
    expect(await within(version).findByText("Version 2.1.0 is available")).toBeInTheDocument();
    expect(within(version).getByText("2.0.0")).toBeInTheDocument();
    expect(within(version).getByText("pip install --upgrade wasm-cli")).toBeInTheDocument();
    expect(within(version).getByRole("link", { name: /What is new in 2.1.0/ })).toHaveAttribute(
      "href",
      "https://github.com/Perkybeet/wasm/releases/tag/v2.1.0",
    );
    expect(screen.getByText("wasm config show")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("does not turn an unsafe release_url into a link", async () => {
    fakeBackend(
      aboutRoutes({
        current_version: "2.0.0",
        latest_version: "2.1.0",
        has_update: true,
        update_command: "pip install --upgrade wasm-cli",
        release_url: "javascript:alert(1)",
      }),
    );
    renderConsole("/settings/about");
    expect(await screen.findByText("Version 2.1.0 is available")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /What is new in/ })).not.toBeInTheDocument();
  });

  it("says when it is up to date, and when it could not tell", async () => {
    const backend = fakeBackend(
      aboutRoutes({ current_version: "2.1.0", latest_version: "2.1.0", has_update: false, update_command: null, release_url: null }),
    );
    const { user } = renderConsole("/settings/about");
    expect(await screen.findByText("Up to date. 2.1.0 is the latest release.")).toBeInTheDocument();

    backend.on("GET /api/system/version", () =>
      json(200, { current_version: "2.1.0", latest_version: null, has_update: false, update_command: null, release_url: null }),
    );
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText("Could not find out whether a newer version exists.")).toBeInTheDocument();
    expect(backend.callsTo("GET /api/system/version")).toHaveLength(2);
  });
});
