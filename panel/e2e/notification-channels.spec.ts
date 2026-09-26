/**
 * Settings > Notifications, the Email and Telegram channels, against the real backend.
 *
 * Email: the SMTP account is a form over GET/PUT /api/config/smtp. A value the configuration
 * refuses lands beside its field in the server's words, and a test that reaches an SMTP server
 * this spec runs - one that refuses to relay - shows that server's own reply verbatim.
 *
 * Telegram: the console server answers the Bot API from its model (console_server.py's
 * TELEGRAM_BOTS), so finding a chat and sending a test never leave the machine. A bot that has
 * seen chats lists them and picking one fills the chat ID; a bot that has seen none explains
 * how to make one appear; a test to a chat the bot is not in shows Telegram's own description.
 *
 * Both write the configuration, so they run on a console server of their own.
 */

import { createServer } from "node:net";
import type { AddressInfo, Socket } from "node:net";

import { expect, expectNoA11yViolations, settle, signIn, startConsoleServer, test as base } from "./fixtures";
import type { ConsoleServer } from "./fixtures";
import { confirmItsYou, stillness, toastSaying } from "./settings.helpers";

const test = base.extend<object, { consoleServer: ConsoleServer }>({
  consoleServer: [
    // eslint-disable-next-line no-empty-pattern -- Playwright requires the destructuring form
    async ({}, use) => {
      const server = await startConsoleServer([]);
      try {
        await use(server);
      } finally {
        await server.stop();
      }
    },
    { scope: "worker", timeout: 75_000 },
  ],
});

/** The relay's refusal, as a mail server words it: what the page must show verbatim. */
const RELAY_DENIED = "5.7.1 Relaying denied: this server does not accept mail from wasm@example.com";

/** A minimal SMTP server on loopback that greets, answers EHLO and refuses every sender. */
async function refusingSmtpServer(): Promise<{ port: number; close: () => Promise<void> }> {
  const sockets = new Set<Socket>();
  const server = createServer((socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
    socket.setEncoding("utf8");
    socket.write("220 relay.test ESMTP\r\n");
    let buffered = "";
    socket.on("data", (chunk: string) => {
      buffered += chunk;
      let end = buffered.indexOf("\r\n");
      while (end !== -1) {
        const line = buffered.slice(0, end).toUpperCase();
        buffered = buffered.slice(end + 2);
        if (line.startsWith("EHLO")) socket.write("250-relay.test\r\n250 8BITMIME\r\n");
        else if (line.startsWith("HELO")) socket.write("250 relay.test\r\n");
        else if (line.startsWith("MAIL FROM")) socket.write(`550 ${RELAY_DENIED}\r\n`);
        else if (line.startsWith("QUIT")) socket.end("221 Bye\r\n");
        else socket.write("250 OK\r\n");
        end = buffered.indexOf("\r\n");
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return {
    port,
    close: () =>
      new Promise((resolve) => {
        for (const socket of sockets) socket.destroy();
        server.close(() => {
          resolve();
        });
      }),
  };
}

test("email: the SMTP account is a form, refusals land on their field, a failed test shows the server's reply", async ({
  page,
  consoleServer,
  problems,
}) => {
  // The first save asks "Confirm it's you" by answering 403 first; the refused host is a 400.
  problems.expect(/status of 403 .* \/api\/config\/smtp$/);
  problems.expect(/status of 400 .* \/api\/config\/smtp$/);
  const relay = await refusingSmtpServer();
  try {
    await signIn(page, consoleServer, "/settings/notifications");
    const email = page.getByRole("article", { name: "Email" });
    const host = email.getByLabel("SMTP server");
    await expect(host).toHaveValue("");
    await expect(email.getByText("Not configured")).toBeVisible();
    await expect(email.getByRole("button", { name: "Send test" })).toBeDisabled();
    await expect(email.getByText("Set up the SMTP server and a recipient to test it.")).toBeVisible();
    await expect(email.getByRole("radio", { name: "SSL/TLS" })).toBeChecked();
    await settle(page);
    await expectNoA11yViolations(page, "the email form, empty");

    // A host the configuration refuses: its words, beside the field.
    await host.fill("smtp example");
    await email.getByRole("radio", { name: "None" }).click();
    await expect(email.getByLabel("Port")).toHaveValue("25");
    await email.getByLabel("Port").fill(String(relay.port));
    await email.getByLabel(/From address/).fill("wasm@example.com");
    const add = email.getByLabel("Add a recipient");
    await add.fill("ops@example");
    await add.press("Enter");
    await expect(email.getByText("ops@example is not an email address. Use one such as ops@example.com.")).toBeVisible();
    await add.fill("ops@example.com");
    await email.getByRole("button", { name: "Add", exact: true }).click();
    await expect(email.getByRole("list", { name: "Recipients" }).getByRole("listitem")).toHaveCount(1);
    await email.getByRole("checkbox", { name: "Send notifications by email" }).click();
    await email.getByRole("button", { name: "Save" }).click();
    await confirmItsYou(page, consoleServer);
    await expect(email.getByText(/^monitor\.smtp\.host is not a valid hostname: .*Use a hostname such as smtp\.example\.com\.$/)).toBeVisible();
    await expect(host).toHaveAttribute("aria-invalid", "true");
    await stillness(page);
    await expectNoA11yViolations(page, "the email form with a refused host");

    // Fixed, it saves, and the password stays write-only.
    await host.fill("127.0.0.1");
    await email.getByRole("button", { name: "Save" }).click();
    await expect(toastSaying(page, "Saved the email settings")).toBeVisible();
    await expect(email.getByText("Configured")).toBeVisible();
    await page.reload();
    const reloaded = page.getByRole("article", { name: "Email" });
    await expect(reloaded.getByLabel("SMTP server")).toHaveValue("127.0.0.1");
    await expect(reloaded.getByLabel("Port")).toHaveValue(String(relay.port));
    await expect(reloaded.getByRole("radio", { name: "None" })).toBeChecked();
    await expect(reloaded.getByRole("checkbox", { name: "Send notifications by email" })).toBeChecked();
    await expect(reloaded.getByLabel(/^Password/)).toHaveValue("");

    // The relay refuses the sender: its own reply, verbatim.
    await reloaded.getByRole("button", { name: "Send test" }).click();
    await expect(reloaded.getByText("The test failed. The SMTP server said:")).toBeVisible();
    await expect(reloaded.locator("pre")).toContainText(RELAY_DENIED);
    await stillness(page);
    await expectNoA11yViolations(page, "the email channel with a failed test");
  } finally {
    await relay.close();
  }
});

test("telegram: finds the chats the bot has seen, fills the chat ID, and shows Telegram's own words", async ({
  page,
  consoleServer,
  problems,
}) => {
  problems.expect(/status of 403 .* \/api\/config\/notifications\/telegram$/);
  problems.expect(/status of 422 .* \/api\/config\/notifications\/telegram$/);
  await signIn(page, consoleServer, "/settings/notifications");
  const telegram = page.getByRole("article", { name: "Telegram" });
  const find = telegram.getByRole("button", { name: "Find my chat" });
  await expect(find).toBeDisabled();
  await expect(telegram.getByText("Save the bot token first.")).toBeVisible();

  // A bot nobody has written to yet: the list is empty, and says how to fill it.
  await telegram.getByLabel("Bot token", { exact: true }).fill("7000000002:console-sandbox-quiet");
  await expect(telegram.getByText("Save the new token first.")).toBeVisible();
  await telegram.getByRole("button", { name: "Save" }).click();
  await confirmItsYou(page, consoleServer);
  await expect(toastSaying(page, "Saved the Telegram destination")).toBeVisible();
  await find.click();
  await expect(telegram.getByText("Your bot has not seen any chat yet.", { exact: true }).last()).toBeVisible();
  await expect(telegram.getByText("/start@your_bot_name")).toBeVisible();
  await stillness(page);
  await expectNoA11yViolations(page, "a bot that has seen no chat");

  // A chat ID Telegram would refuse: the server's words beside the field.
  await telegram.getByLabel("Chat ID").fill("1001987654321");
  await telegram.getByRole("button", { name: "Save" }).click();
  await expect(telegram.getByText(/did you mean -1001987654321\?/)).toBeVisible();
  await expect(telegram.getByLabel("Chat ID")).toHaveAttribute("aria-invalid", "true");

  // A chat the bot is not in: the test shows Telegram's own description.
  await telegram.getByLabel("Chat ID").fill("-100123");
  await telegram.getByRole("button", { name: "Save" }).click();
  // Saved once the form has nothing left to save (earlier toasts may still be on screen).
  await expect(telegram.getByRole("button", { name: "Save" })).toBeHidden();
  await telegram.getByRole("button", { name: "Send test" }).click();
  await expect(telegram.getByText("The test failed. Telegram said:")).toBeVisible();
  await expect(telegram.locator("pre")).toContainText("Bad Request: chat not found");

  // The bot that has seen chats: pick the group, save, and the test goes through.
  await telegram.getByLabel("Bot token", { exact: true }).fill("7000000001:console-sandbox-bot");
  await telegram.getByRole("button", { name: "Save" }).click();
  // Saved once the form has nothing left to save (earlier toasts may still be on screen).
  await expect(telegram.getByRole("button", { name: "Save" })).toBeHidden();
  await find.click();
  const chats = telegram.getByRole("list", { name: "Chats your bot has seen" });
  await expect(chats.getByRole("listitem")).toHaveCount(2);
  await expect(chats.getByText("WASM alerts")).toBeVisible();
  await expect(chats.getByText("Supergroup")).toBeVisible();
  await expect(chats.getByText("@ops_oncall")).toBeVisible();
  await stillness(page);
  await expectNoA11yViolations(page, "the chats a bot has seen");
  await chats.getByRole("button", { name: "Use WASM alerts" }).click();
  await expect(telegram.getByLabel("Chat ID")).toHaveValue("-1001987654321");
  await expect(chats.getByText("Chosen")).toBeVisible();
  await telegram.getByRole("button", { name: "Save" }).click();
  // Saved once the form has nothing left to save (earlier toasts may still be on screen).
  await expect(telegram.getByRole("button", { name: "Save" })).toBeHidden();
  await telegram.getByRole("button", { name: "Send test" }).click();
  await expect(telegram.getByText("Test message sent through telegram.")).toBeVisible();
});
