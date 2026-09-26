/**
 * Settings > Notifications against the real backend and a real receiving endpoint.
 *
 * The notifier refuses private destinations, so the test lists 127.0.0.1 as an allowed
 * private host (through the page), points the webhook channel at an HTTP listener this test
 * runs, and sends a test: the listener receives the notifier's JSON and the page reports the
 * server's answer. A channel with no destination and a refused private address report their
 * failure verbatim. No request leaves the machine.
 */

import { createServer } from "node:http";
import type { AddressInfo } from "node:net";

import { expect, expectNoA11yViolations, settle, signIn, test } from "./fixtures";
import { confirmItsYou, stillness, toastSaying } from "./settings.helpers";

interface Received {
  path: string;
  body: Record<string, unknown>;
}

/** A local endpoint that records what it is sent and answers 204. */
async function listen(): Promise<{ url: string; received: Received[]; close: () => Promise<void> }> {
  const received: Received[] = [];
  const server = createServer((request, response) => {
    let text = "";
    request.setEncoding("utf8");
    request.on("data", (chunk: string) => {
      text += chunk;
    });
    request.on("end", () => {
      received.push({ path: request.url ?? "", body: JSON.parse(text || "{}") as Record<string, unknown> });
      response.writeHead(204).end();
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return {
    url: `http://127.0.0.1:${String(port)}/hooks/wasm`,
    received,
    close: () => new Promise((resolve) => server.close(() => { resolve(); })),
  };
}

test("tests each channel and reports what the receiving server answered", async ({ page, consoleServer, problems }) => {
  // Writing configuration asks "Confirm it's you" by answering 403 first, by design.
  problems.expect(/status of 403 .* \/api\/config$/);
  const endpoint = await listen();
  try {
    await signIn(page, consoleServer, "/settings/notifications");
    await expect(page.getByRole("heading", { level: 2, name: "Where alerts go" })).toBeVisible();
    await settle(page);
    await expectNoA11yViolations(page, "notifications");

    // A channel with no destination cannot be tested: disabled, with why, next to a channel
    // that already has one - the two states shown side by side, and both pass axe.
    const slack = page.getByRole("article", { name: "Slack" });
    await expect(slack.getByText("Not configured")).toBeVisible();
    await expect(slack.getByRole("button", { name: "Send test" })).toBeDisabled();
    await expect(slack.getByText("Add a destination to test it.")).toBeVisible();
    const email = page.getByRole("article", { name: "Email" });
    await expect(email.getByText("Not configured")).toBeVisible();
    await expect(email.getByRole("button", { name: "Send test" })).toBeDisabled();
    await expect(email.getByText("Set up the SMTP server to test it.")).toBeVisible();
    await expectNoA11yViolations(page, "unconfigured channels");

    // Allow this machine as a destination, which the SSRF guard refuses otherwise.
    const privateHosts = page.getByRole("region", { name: "Private destinations" });
    await privateHosts.getByLabel(/Allowed private hosts/).fill("127.0.0.1");
    await privateHosts.getByRole("button", { name: "Save changes" }).click();
    await confirmItsYou(page, consoleServer);
    await expect(toastSaying(page, "Saved the private destinations")).toBeVisible();

    // Point the webhook at the listener; the secret field is write-only.
    const webhook = page.getByRole("article", { name: "Webhook" });
    const url = webhook.getByLabel("Endpoint URL", { exact: true });
    await expect(url).toHaveValue("");
    await expect(url).toHaveAttribute("type", "password");
    await url.fill(endpoint.url);
    await expect(webhook.getByRole("button", { name: "Send test" })).toBeDisabled();
    await webhook.getByRole("button", { name: "Save" }).click();
    await expect(toastSaying(page, "Saved the Webhook destination")).toBeVisible();
    await expect(url).toHaveValue("");
    await expect(webhook.getByText("Configured")).toBeVisible();
    await expect(url).toHaveAttribute("placeholder", "Set - leave blank to keep it");

    await webhook.getByRole("button", { name: "Send test" }).click();
    await expect(webhook.getByText("Test message sent through webhook.")).toBeVisible();
    expect(endpoint.received).toHaveLength(1);
    expect(endpoint.received[0]?.path).toBe("/hooks/wasm");
    expect(endpoint.received[0]?.body).toMatchObject({ event: "test", title: "WASM test notification" });

    // A private address that is not allowed is refused before any request is made.
    const discord = page.getByRole("article", { name: "Discord" });
    await discord.getByLabel("Webhook URL", { exact: true }).fill("http://10.20.30.40/hook");
    await discord.getByRole("button", { name: "Save" }).click();
    await expect(toastSaying(page, "Saved the Discord destination")).toBeVisible();
    await expect(discord.getByText("Configured")).toBeVisible();
    await discord.getByRole("button", { name: "Send test" }).click();
    await expect(discord.getByText("The test failed. The server said:")).toBeVisible();
    await expect(discord.locator("pre")).toContainText("allow_private_hosts");
    await stillness(page);
    await expectNoA11yViolations(page, "notifications with test results");

    // The master switch and the events, then everything back as it was.
    const toggle = page.getByRole("switch", { name: /Send notifications/ });
    await expect(toggle).not.toBeChecked();
    await toggle.click();
    await expect(toastSaying(page, "Turned notifications on")).toBeVisible();
    await expect(toggle).toBeChecked();
    await toggle.click();
    await expect(toastSaying(page, "Turned notifications off")).toBeVisible();
    await expect(toggle).not.toBeChecked();

    const events = page.getByRole("region", { name: "Events" });
    await events.getByRole("checkbox", { name: /Deploy finished/ }).click();
    await expect(events.getByText("wasm config set notifications.events.deploy_success false")).toBeVisible();
    await events.getByRole("button", { name: "Save changes" }).click();
    await expect(toastSaying(page, "Saved the notification events")).toBeVisible();
    await page.reload();
    await expect(page.getByRole("region", { name: "Events" }).getByRole("checkbox", { name: /Deploy finished/ })).not.toBeChecked();
    await page.getByRole("region", { name: "Events" }).getByRole("checkbox", { name: /Deploy finished/ }).click();
    await page.getByRole("region", { name: "Events" }).getByRole("button", { name: "Save changes" }).click();
    await expect(toastSaying(page, "Saved the notification events")).toBeVisible();

    for (const name of ["Webhook", "Discord"]) {
      await page.getByRole("article", { name }).getByRole("button", { name: "Remove destination" }).click();
      await expect(toastSaying(page, `Removed the ${name} destination`)).toBeVisible();
    }
    const hosts = page.getByRole("region", { name: "Private destinations" });
    await hosts.getByLabel(/Allowed private hosts/).fill("");
    await hosts.getByRole("button", { name: "Save changes" }).click();
    await expect(toastSaying(page, "Saved the private destinations")).toBeVisible();

    const webhookAfter = page.getByRole("article", { name: "Webhook" });
    await expect(webhookAfter.getByText("Not configured")).toBeVisible();
    await expect(webhookAfter.getByRole("button", { name: "Send test" })).toBeDisabled();
    await settle(page);
    await expectNoA11yViolations(page, "channels after removing their destinations");
  } finally {
    await endpoint.close();
  }
});
