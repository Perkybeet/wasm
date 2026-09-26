import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";

/** The notifications block as GET /api/config answers it: every secret redacted, set or not. */
function configBody(notifications: Record<string, unknown> = {}) {
  return {
    config: {
      notifications: {
        enabled: false,
        events: {
          deploy_success: true,
          deploy_failed: true,
          cert_expiring: true,
          unit_failed: true,
          disk_threshold: true,
          backup_failed: true,
        },
        channels: {
          webhook: { webhook_url: "***" },
          slack: { webhook_url: "***" },
          discord: { webhook_url: "***" },
          telegram: { bot_token: "***", chat_id: "" },
          email: { enabled: false },
        },
        ...notifications,
      },
      monitor: { smtp: { host: "", port: 465, from_address: "", password: "***" }, email_recipients: [] },
    },
    path: "/etc/wasm/config.yaml",
    writable: true,
  };
}

function notificationsBackend() {
  let elevated = false;
  return fakeBackend({
    ...signedInRoutes(),
    "GET /api/config": () => json(200, configBody()),
    "POST /api/auth/elevate": () => {
      elevated = true;
      return json(200, { elevated_until: new Date(Date.now() + 600_000).toISOString() });
    },
    "PATCH /api/config": (call) => {
      if (!elevated) return problem(403, "elevation_required", "Confirm it's you to continue.");
      const body = call.body as { path: string; value: unknown };
      return json(200, { message: `Configuration '${body.path}' updated`, path: "/etc/wasm/config.yaml", value: body.value });
    },
    "POST /api/config/notifications/slack/test": () =>
      json(200, { ok: false, detail: "Channel slack is not configured; set notifications.channels.slack.webhook_url first." }),
    "POST /api/config/notifications/webhook/test": () => json(200, { ok: true, detail: "Test message sent through webhook." }),
  });
}

function channel(name: string): HTMLElement {
  return screen.getByRole("article", { name });
}

async function confirmItsYou(user: ReturnType<typeof renderConsole>["user"]): Promise<void> {
  const confirm = await screen.findByRole("dialog", { name: "Confirm it's you" });
  await user.type(within(confirm).getByLabelText("Authentication code"), "123456");
  await user.click(within(confirm).getByRole("button", { name: "Confirm" }));
}

describe("Settings > Notifications", () => {
  it("shows every channel, the events and the master switch, and passes axe", { timeout: 20_000 }, async () => {
    notificationsBackend();
    const { container } = renderConsole("/settings/notifications");
    expect(await screen.findByRole("switch", { name: /Send notifications/ })).not.toBeChecked();
    for (const name of ["Webhook", "Slack", "Discord", "Telegram", "Email"]) expect(channel(name)).toBeInTheDocument();
    // Secrets are write-only: the field starts empty and says so.
    const url = within(channel("Slack")).getByLabelText("Incoming webhook URL");
    expect(url).toHaveValue("");
    expect(url).toHaveAttribute("type", "password");
    expect(within(channel("Email")).getByText("Not set up")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Certificate expiring/ })).toBeInTheDocument();
    expect(screen.getByText(/Not sent by this version of WASM yet/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("tests a channel and shows what the server answered, verbatim", async () => {
    notificationsBackend();
    const { user } = renderConsole("/settings/notifications");
    await screen.findByRole("switch", { name: /Send notifications/ });
    await user.click(within(channel("Slack")).getByRole("button", { name: "Send test" }));
    expect(
      await within(channel("Slack")).findByText("Channel slack is not configured; set notifications.channels.slack.webhook_url first."),
    ).toBeInTheDocument();
    expect(within(channel("Slack")).getByText("The test failed. The server said:")).toBeInTheDocument();

    await user.click(within(channel("Webhook")).getByRole("button", { name: "Send test" }));
    expect(await within(channel("Webhook")).findByText("Test message sent through webhook.")).toBeInTheDocument();
  });

  it("saves a destination after confirming it's you, keeping the secrets it was not given", { timeout: 20_000 }, async () => {
    const backend = notificationsBackend();
    const { user } = renderConsole("/settings/notifications");
    await screen.findByRole("switch", { name: /Send notifications/ });
    const telegram = channel("Telegram");
    await user.type(within(telegram).getByLabelText("Chat ID"), "-1001234");
    // An unsaved destination is not what a test would reach.
    expect(within(telegram).getByRole("button", { name: "Send test" })).toBeDisabled();
    await user.click(within(telegram).getByRole("button", { name: "Save" }));
    await confirmItsYou(user);
    await waitFor(() => {
      expect(backend.callsTo("PATCH /api/config").at(-1)?.body).toEqual({
        path: "notifications.channels.telegram",
        value: { bot_token: "***", chat_id: "-1001234" },
      });
    });

    const slack = channel("Slack");
    await user.type(within(slack).getByLabelText("Incoming webhook URL"), "https://hooks.slack.com/services/T0/B0/x");
    await user.click(within(slack).getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(backend.callsTo("PATCH /api/config").at(-1)?.body).toEqual({
        path: "notifications.channels.slack",
        value: { webhook_url: "https://hooks.slack.com/services/T0/B0/x" },
      });
    });

    await user.click(within(channel("Discord")).getByRole("button", { name: "Remove destination" }));
    await waitFor(() => {
      expect(backend.callsTo("PATCH /api/config").at(-1)?.body).toEqual({
        path: "notifications.channels.discord",
        value: { webhook_url: "" },
      });
    });
  });

  it("tells a configured channel from an unconfigured one, and disables its test", async () => {
    notificationsBackend();
    const { container } = renderConsole("/settings/notifications");
    await screen.findByRole("switch", { name: /Send notifications/ });

    // Webhook has a stored "***": configured, its secret field says so, and it can be tested.
    const webhook = channel("Webhook");
    expect(within(webhook).getByText("Configured")).toBeInTheDocument();
    expect(within(webhook).getByLabelText("Endpoint URL")).toHaveAttribute("placeholder", "Set - leave blank to keep it");
    expect(within(webhook).getByRole("button", { name: "Send test" })).toBeEnabled();

    // Email's destination is the monitor's SMTP host, which the seed leaves blank.
    const email = channel("Email");
    expect(within(email).getByText("Not configured")).toBeInTheDocument();
    const emailTest = within(email).getByRole("button", { name: "Send test" });
    expect(emailTest).toBeDisabled();
    expect(within(email).getByText("Set up the SMTP server to test it.")).toBeInTheDocument();

    await expectNoAxeViolations(container);
  });

  it("hints that a group's chat ID is negative, and warns before saving one typed without its sign", async () => {
    notificationsBackend();
    const { user, container } = renderConsole("/settings/notifications");
    await screen.findByRole("switch", { name: /Send notifications/ });
    const telegram = channel("Telegram");
    const chatId = within(telegram).getByLabelText("Chat ID");

    expect(
      within(telegram).getByText(
        "A group or supergroup's chat ID is negative, and a supergroup's starts with -100. A personal chat's is a smaller positive number.",
      ),
    ).toBeInTheDocument();
    expect(within(telegram).queryByRole("alert")).toBeNull();

    // A personal chat's ID is a short positive number: no warning.
    await user.type(chatId, "123456789");
    expect(within(telegram).queryByRole("alert")).toBeNull();

    // 13+ digits, positive: the shape of a supergroup ID typed without its leading minus sign.
    await user.clear(chatId);
    await user.type(chatId, "1001234567890");
    expect(
      await within(telegram).findByText(
        "This looks like a group's chat ID without its minus sign. Groups and supergroups use a negative ID (a supergroup's starts with -100); try -1001234567890.",
      ),
    ).toBeInTheDocument();
    await expectNoAxeViolations(container);

    // The warning does not block saving: it is a hint, not a validation failure.
    await user.click(within(telegram).getByRole("button", { name: "Save" }));
    await confirmItsYou(user);
    await waitFor(() => {
      expect(within(telegram).queryByRole("button", { name: "Save" })).toBeNull();
    });
  });

  it("turns delivery on and saves the events as one map", { timeout: 20_000 }, async () => {
    const backend = notificationsBackend();
    const { user } = renderConsole("/settings/notifications");
    await user.click(await screen.findByRole("switch", { name: /Send notifications/ }));
    await confirmItsYou(user);
    await waitFor(() => {
      expect(backend.callsTo("PATCH /api/config")[1]?.body).toEqual({ path: "notifications.enabled", value: true });
    });

    await user.click(screen.getByRole("checkbox", { name: /Deploy finished/ }));
    const events = screen.getByRole("region", { name: "Events" });
    expect(within(events).getByText("wasm config set notifications.events.deploy_success false")).toBeInTheDocument();
    await user.click(within(events).getByRole("button", { name: "Save changes" }));
    await waitFor(() => {
      expect(backend.callsTo("PATCH /api/config").at(-1)?.body).toEqual({
        path: "notifications.events",
        value: {
          deploy_success: false,
          deploy_failed: true,
          cert_expiring: true,
          unit_failed: true,
          disk_threshold: true,
          backup_failed: true,
        },
      });
    });
  });

  it("saves the private destinations as a list", async () => {
    const backend = notificationsBackend();
    const { user } = renderConsole("/settings/notifications");
    const hosts = await screen.findByLabelText(/Allowed private hosts/);
    await user.type(hosts, "10.0.0.12{Enter}hooks.internal");
    await user.click(within(screen.getByRole("region", { name: "Private destinations" })).getByRole("button", { name: "Save changes" }));
    await confirmItsYou(user);
    await waitFor(() => {
      expect(backend.callsTo("PATCH /api/config").at(-1)?.body).toEqual({
        path: "notifications.allow_private_hosts",
        value: ["10.0.0.12", "hooks.internal"],
      });
    });
  });
});
