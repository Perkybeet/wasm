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

/** GET /api/config/smtp: the typed SMTP account, the password only as "one is stored". */
const SMTP_UNSET = {
  host: "",
  port: 465,
  use_ssl: true,
  use_tls: false,
  username: "",
  from_address: "",
  recipients: [],
  password_set: false,
};

const SMTP_SET = {
  host: "smtp.example.com",
  port: 465,
  use_ssl: true,
  use_tls: false,
  username: "wasm@example.com",
  from_address: "wasm@example.com",
  recipients: ["ops@example.com"],
  password_set: true,
};

function notificationsBackend(
  options: { smtp?: typeof SMTP_UNSET | typeof SMTP_SET; config?: ReturnType<typeof configBody>; routes?: Parameters<typeof fakeBackend>[0] } = {},
) {
  let elevated = false;
  const needsElevation = () => (elevated ? null : problem(403, "elevation_required", "Confirm it's you to continue."));
  return fakeBackend({
    ...signedInRoutes(),
    "GET /api/config": () => json(200, options.config ?? configBody()),
    "GET /api/config/smtp": () => json(200, options.smtp ?? SMTP_UNSET),
    "PUT /api/config/smtp": () => needsElevation() ?? json(200, { message: "SMTP configuration updated" }),
    "PUT /api/config/notifications/telegram": () => needsElevation() ?? json(200, { message: "Telegram configuration updated" }),
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
    ...options.routes,
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
    expect(await within(channel("Email")).findByLabelText("SMTP server")).toHaveValue("");
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
    // Telegram's own endpoint: an empty token keeps the stored one.
    await waitFor(() => {
      expect(backend.callsTo("PUT /api/config/notifications/telegram").at(-1)?.body).toEqual({ bot_token: "", chat_id: "-1001234" });
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
    const emailTest = await within(email).findByRole("button", { name: "Send test" });
    await waitFor(() => {
      expect(emailTest).toBeDisabled();
    });
    expect(within(email).getByText("Set up the SMTP server and a recipient to test it.")).toBeInTheDocument();

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
    // The warning is drawn as it is typed (so it never pushes the Save button away from a
    // click that leaves the field), but it is not a live region: one that re-renders on every
    // digit would be read out on every digit. Leaving the field says it, once.
    await user.clear(chatId);
    await user.type(chatId, "1001234567890");
    const warning =
      "This looks like a group's chat ID without its minus sign. Groups and supergroups use a negative ID (a supergroup's starts with -100); try -1001234567890.";
    expect(within(telegram).getByText(warning).closest("[role=alert], [role=status], [aria-live]")).toBeNull();
    const announcer = screen.getByTestId("announcer-polite");
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(announcer).toHaveTextContent("");

    await user.tab();
    await waitFor(() => {
      expect(announcer).toHaveTextContent(warning);
    });
    await expectNoAxeViolations(container);

    // A fixed value clears the warning; breaking it again brings it back.
    await user.click(chatId);
    await user.type(chatId, "{Home}-");
    expect(within(telegram).queryByText(warning)).toBeNull();
    await user.click(chatId);
    await user.type(chatId, "{Home}{Delete}");
    expect(within(telegram).getByText(warning)).toBeInTheDocument();

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
  it("sets up the SMTP account and its recipients as one save, after confirming it's you", { timeout: 30_000 }, async () => {
    const backend = notificationsBackend();
    const { user, container } = renderConsole("/settings/notifications");
    await screen.findByRole("switch", { name: /Send notifications/ });
    const email = channel("Email");
    await user.type(await within(email).findByLabelText("SMTP server"), "smtp.example.com");

    // SSL/TLS and STARTTLS are one choice; the usual port follows it unless one was typed.
    const encryption = within(email).getByRole("radiogroup", { name: "Encryption" });
    expect(within(encryption).getByRole("radio", { name: "SSL/TLS" })).toBeChecked();
    await user.click(within(encryption).getByRole("radio", { name: "STARTTLS" }));
    expect(within(email).getByLabelText("Port")).toHaveValue("587");

    await user.type(within(email).getByLabelText(/Username/), "wasm@example.com");
    const password = within(email).getByLabelText(/^Password/);
    expect(password).toHaveAttribute("type", "password");
    await user.type(password, "s3cret");
    await user.type(within(email).getByLabelText(/From address/), "wasm@example.com");

    // Each address is checked as it is added; a mistake stays in the box with why.
    const add = within(email).getByLabelText("Add a recipient");
    await user.type(add, "ops@example{Enter}");
    expect(within(email).getByText("ops@example is not an email address. Use one such as ops@example.com.")).toBeInTheDocument();
    expect(add).toHaveValue("ops@example");
    await user.clear(add);
    await user.type(add, "ops@example.com, dev@example.com{Enter}");
    const recipients = within(email).getByRole("list", { name: "Recipients" });
    expect(within(recipients).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["ops@example.com", "dev@example.com"]);
    await user.click(within(recipients).getByRole("button", { name: "Remove dev@example.com" }));
    expect(within(recipients).getAllByRole("listitem")).toHaveLength(1);

    await user.click(within(email).getByRole("checkbox", { name: "Send notifications by email" }));
    await expectNoAxeViolations(container);
    await user.click(within(email).getByRole("button", { name: "Save" }));
    await confirmItsYou(user);
    await waitFor(() => {
      expect(backend.callsTo("PATCH /api/config").at(-1)?.body).toEqual({ path: "notifications.channels.email", value: { enabled: true } });
    });
    expect(backend.callsTo("PUT /api/config/smtp").at(-1)?.body).toEqual({
      host: "smtp.example.com",
      port: 587,
      use_ssl: false,
      use_tls: true,
      username: "wasm@example.com",
      password: "s3cret",
      from_address: "wasm@example.com",
      recipients: ["ops@example.com"],
    });
  });

  it("puts the server's refusal beside the field it is about, verbatim", { timeout: 20_000 }, async () => {
    let answer = problem(400, "config_error", "monitor.smtp.host is not a valid hostname: Invalid domain format", {
      hint: "Got 'smtp example'. Use a hostname such as smtp.example.com.",
    });
    const backend = notificationsBackend({
      routes: {
        "POST /api/auth/elevate": () => json(200, { elevated_until: new Date(Date.now() + 600_000).toISOString() }),
        "PUT /api/config/smtp": () => answer,
      },
    });
    const { user, container } = renderConsole("/settings/notifications");
    await screen.findByRole("switch", { name: /Send notifications/ });
    const email = channel("Email");
    const host = await within(email).findByLabelText("SMTP server");
    await user.type(host, "smtp example");
    await user.click(within(email).getByRole("button", { name: "Save" }));
    expect(
      await within(email).findByText("monitor.smtp.host is not a valid hostname: Invalid domain format Got 'smtp example'. Use a hostname such as smtp.example.com."),
    ).toBeInTheDocument();
    expect(host).toHaveAttribute("aria-invalid", "true");
    await expectNoAxeViolations(container);

    // A 422 names the field itself.
    answer = problem(422, "validation_error", "Validation failed", { fields: { port: "Input should be less than or equal to 65535" } });
    await user.clear(within(email).getByLabelText("Port"));
    await user.type(within(email).getByLabelText("Port"), "99999");
    await user.click(within(email).getByRole("button", { name: "Save" }));
    expect(await within(email).findByText("Input should be less than or equal to 65535")).toBeInTheDocument();
    expect(within(email).getByLabelText("Port")).toHaveAttribute("aria-invalid", "true");
    expect(backend.callsTo("PUT /api/config/smtp")).toHaveLength(2);

    // A refused recipient is marked where it stands.
    answer = problem(400, "config_error", "monitor.email_recipients contains an invalid email address: ops@localhost.x", {
      hint: "Every recipient must be an address such as ops@example.com.",
    });
    await user.type(within(email).getByLabelText("Add a recipient"), "ops@localhost.x{Enter}");
    await user.click(within(email).getByRole("button", { name: "Save" }));
    const recipients = await within(email).findByRole("list", { name: "Recipients" });
    expect(await within(recipients).findByText("Refused")).toBeInTheDocument();
    expect(within(email).getByText(/^monitor\.email_recipients contains an invalid email address: ops@localhost\.x/)).toBeInTheDocument();
  });

  it("keeps a stored password write-only and shows the SMTP server's own words when a test fails", async () => {
    notificationsBackend({
      smtp: SMTP_SET,
      config: configBody({ channels: { ...configBody().config.notifications.channels, email: { enabled: true } } }),
      routes: {
        "POST /api/config/notifications/email/test": () =>
          json(200, {
            ok: false,
            detail: "SMTP authentication failed\n  Details: Check monitor.smtp.username and password: (535, b'5.7.8 Username and Password not accepted')",
          }),
      },
    });
    const { user } = renderConsole("/settings/notifications");
    await screen.findByRole("switch", { name: /Send notifications/ });
    const email = channel("Email");
    const password = await within(email).findByLabelText(/^Password/);
    expect(password).toHaveValue("");
    expect(password).toHaveAttribute("placeholder", "Set - leave blank to keep it");
    expect(within(email).getByText("Configured")).toBeInTheDocument();
    await user.click(within(email).getByRole("button", { name: "Send test" }));
    expect(await within(email).findByText("The test failed. The SMTP server said:")).toBeInTheDocument();
    expect(within(email).getByText(/535, b'5\.7\.8 Username and Password not accepted'/)).toBeInTheDocument();
  });

  it("finds the chats the bot has seen and fills the chat ID with the one picked", { timeout: 20_000 }, async () => {
    const backend = notificationsBackend({
      routes: {
        "POST /api/config/notifications/telegram/chats": () =>
          json(200, {
            chats: [
              { id: -1001987654321, type: "supergroup", title: "WASM alerts", username: null },
              { id: 52345678, type: "private", title: null, username: "yago" },
            ],
          }),
      },
    });
    const { user, container } = renderConsole("/settings/notifications");
    await screen.findByRole("switch", { name: /Send notifications/ });
    const telegram = channel("Telegram");
    await user.click(within(telegram).getByRole("button", { name: "Find my chat" }));
    const chats = await within(telegram).findByRole("list", { name: "Chats your bot has seen" });
    expect(within(chats).getByText("WASM alerts")).toBeInTheDocument();
    expect(within(chats).getByText("Supergroup")).toBeInTheDocument();
    expect(within(chats).getByText("-1001987654321")).toBeInTheDocument();
    expect(within(chats).getByText("@yago")).toBeInTheDocument();
    expect(within(chats).getByText("Private chat")).toBeInTheDocument();
    expect(screen.getByText("Found 2 chats.")).toBeInTheDocument();
    await expectNoAxeViolations(container);

    await user.click(within(chats).getByRole("button", { name: "Use WASM alerts" }));
    expect(within(telegram).getByLabelText("Chat ID")).toHaveValue("-1001987654321");
    expect(within(chats).getByText("Chosen")).toBeInTheDocument();
    await user.click(within(telegram).getByRole("button", { name: "Save" }));
    await confirmItsYou(user);
    await waitFor(() => {
      expect(backend.callsTo("PUT /api/config/notifications/telegram").at(-1)?.body).toEqual({ bot_token: "", chat_id: "-1001987654321" });
    });
  });

  it("explains how to make a chat appear when the bot has seen none, and needs a saved token", async () => {
    const withoutToken = configBody({ channels: { ...configBody().config.notifications.channels, telegram: { bot_token: "", chat_id: "" } } });
    notificationsBackend({ config: withoutToken });
    const first = renderConsole("/settings/notifications");
    await screen.findByRole("switch", { name: /Send notifications/ });
    expect(within(channel("Telegram")).getByRole("button", { name: "Find my chat" })).toBeDisabled();
    expect(within(channel("Telegram")).getByText("Save the bot token first.")).toBeInTheDocument();
    await first.user.type(within(channel("Telegram")).getByLabelText("Bot token"), "123:abc");
    expect(within(channel("Telegram")).getByText("Save the new token first.")).toBeInTheDocument();
    first.unmount();

    notificationsBackend({ routes: { "POST /api/config/notifications/telegram/chats": () => json(200, { chats: [] }) } });
    const { user } = renderConsole("/settings/notifications");
    await screen.findByRole("switch", { name: /Send notifications/ });
    await user.click(within(channel("Telegram")).getByRole("button", { name: "Find my chat" }));
    expect(await within(channel("Telegram")).findByText(/Add the bot to the group and send/)).toBeInTheDocument();
    expect(within(channel("Telegram")).getByText("/start@your_bot_name")).toBeInTheDocument();
  });

  it("shows why the chats could not be listed, and Telegram's own words when a test fails", async () => {
    notificationsBackend({
      config: configBody({ channels: { ...configBody().config.notifications.channels, telegram: { bot_token: "***", chat_id: "-100123" } } }),
      routes: {
        "POST /api/config/notifications/telegram/chats": () => problem(400, "validation_error", "HTTP Error 401: Unauthorized"),
        "POST /api/config/notifications/telegram/test": () => json(200, { ok: false, detail: "HTTP 400 Bad Request: Bad Request: chat not found" }),
      },
    });
    const { user } = renderConsole("/settings/notifications");
    await screen.findByRole("switch", { name: /Send notifications/ });
    const telegram = channel("Telegram");
    await user.click(within(telegram).getByRole("button", { name: "Find my chat" }));
    expect(await within(telegram).findByText("Could not list the chats")).toBeInTheDocument();
    expect(within(telegram).getByText("HTTP Error 401: Unauthorized")).toBeInTheDocument();

    await user.click(within(telegram).getByRole("button", { name: "Send test" }));
    expect(await within(telegram).findByText("The test failed. Telegram said:")).toBeInTheDocument();
    expect(within(telegram).getByText("HTTP 400 Bad Request: Bad Request: chat not found")).toBeInTheDocument();
  });

  it("puts a chat ID Telegram would refuse beside its field", { timeout: 20_000 }, async () => {
    notificationsBackend({
      routes: {
        "POST /api/auth/elevate": () => json(200, { elevated_until: new Date(Date.now() + 600_000).toISOString() }),
        "PUT /api/config/notifications/telegram": () =>
          problem(422, "validation_error", "Validation failed", {
            fields: { chat_id: "Value error, 1001234567890 looks like a supergroup or channel id missing its leading '-'" },
          }),
      },
    });
    const { user } = renderConsole("/settings/notifications");
    await screen.findByRole("switch", { name: /Send notifications/ });
    const telegram = channel("Telegram");
    await user.type(within(telegram).getByLabelText("Chat ID"), "1001234567890");
    await user.click(within(telegram).getByRole("button", { name: "Save" }));
    expect(await within(telegram).findByText(/looks like a supergroup or channel id missing its leading '-'/)).toBeInTheDocument();
    expect(within(telegram).getByLabelText("Chat ID")).toHaveAttribute("aria-invalid", "true");
  });
});
