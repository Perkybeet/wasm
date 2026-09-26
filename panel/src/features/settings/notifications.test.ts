import { describe, expect, it } from "vitest";

import {
  CHANNELS,
  REDACTED,
  channelValue,
  isChannelConfigured,
  parseHostList,
  looksLikeEmail,
  portForSecurity,
  readNotificationSettings,
  refusedRecipients,
  smtpBody,
  smtpFormDirty,
  smtpFormFrom,
  splitAddresses,
  telegramChatIdWarning,
  telegramChatName,
  telegramChatType,
} from "./notifications";
import type { ChannelSpec } from "./notifications";

/** GET /api/config's notifications block as the sandboxed server answers it. */
const CONFIG = {
  notifications: {
    enabled: true,
    events: { deploy_success: false, deploy_failed: true },
    channels: {
      webhook: { webhook_url: "***" },
      slack: { webhook_url: "***" },
      discord: { webhook_url: "***" },
      telegram: { bot_token: "***", chat_id: "-1001234" },
      email: { enabled: true },
    },
    allow_private_hosts: ["10.0.0.12"],
  },
  monitor: { smtp: { host: "smtp.example.com", port: 465, from_address: "wasm@example.com", password: "***" }, email_recipients: ["ops@example.com"] },
};

function spec(id: string): ChannelSpec {
  const found = CHANNELS.find((channel) => channel.id === id);
  if (!found) throw new Error(id);
  return found;
}

describe("the notification settings", () => {
  it("reads the block, treating an event missing from the file as on, like the notifier", () => {
    const settings = readNotificationSettings(CONFIG);
    expect(settings.enabled).toBe(true);
    expect(settings.events).toMatchObject({ deploy_success: false, deploy_failed: true, unit_failed: true, backup_failed: true });
    expect(settings.channels.telegram).toEqual({ bot_token: REDACTED, chat_id: "-1001234" });
    expect(settings.emailEnabled).toBe(true);
    expect(settings.allowPrivateHosts).toEqual(["10.0.0.12"]);
    expect(settings.smtp).toEqual({ host: "smtp.example.com", port: 465, from: "wasm@example.com", recipients: ["ops@example.com"] });
  });

  it("reads an empty configuration without inventing anything", () => {
    const settings = readNotificationSettings({});
    expect(settings.enabled).toBe(false);
    expect(settings.channels.slack).toEqual({ webhook_url: "" });
    expect(settings.smtp.host).toBe("");
  });

  it("sends a secret left empty back as the placeholder, so the stored one is kept", () => {
    expect(channelValue(spec("slack"), { webhook_url: "" }, {})).toEqual({ webhook_url: REDACTED });
    expect(channelValue(spec("telegram"), { bot_token: REDACTED, chat_id: "-1001234" }, { chat_id: " 42 " })).toEqual({
      bot_token: REDACTED,
      chat_id: "42",
    });
  });

  it("sends what was typed, and clears on request", () => {
    expect(channelValue(spec("webhook"), { webhook_url: "" }, { webhook_url: "https://hooks.example.com/x " })).toEqual({
      webhook_url: "https://hooks.example.com/x",
    });
    expect(
      channelValue(spec("telegram"), { bot_token: REDACTED, chat_id: "-1001234" }, { chat_id: "42" }, new Set(["bot_token"])),
    ).toEqual({ bot_token: "", chat_id: "42" });
  });

  it("keeps a field the operator left alone, even when a sibling field in the same channel changed", () => {
    const stored = { bot_token: REDACTED, chat_id: "-1001234" };
    // Only the token was touched: the chat ID must round-trip untouched, not blank.
    expect(channelValue(spec("telegram"), stored, { bot_token: "a-fresh-token" })).toEqual({
      bot_token: "a-fresh-token",
      chat_id: "-1001234",
    });
  });

  it("reports whether a channel has a destination configured, from the redacted secret alone", () => {
    expect(isChannelConfigured(spec("slack"), { webhook_url: "" })).toBe(false);
    expect(isChannelConfigured(spec("slack"), { webhook_url: REDACTED })).toBe(true);
    expect(isChannelConfigured(spec("telegram"), { bot_token: "", chat_id: "-1001234" })).toBe(false);
    expect(isChannelConfigured(spec("telegram"), { bot_token: REDACTED, chat_id: "-1001234" })).toBe(true);
  });

  it("reads a list of hosts typed one per line or with commas", () => {
    expect(parseHostList("10.0.0.12\n  Hooks.Internal , 10.0.0.12\n\n")).toEqual(["10.0.0.12", "hooks.internal"]);
    expect(parseHostList("   ")).toEqual([]);
  });
});

describe("telegramChatIdWarning", () => {
  it("says nothing about a negative ID, the correct shape for a group or supergroup", () => {
    expect(telegramChatIdWarning("-1001234567890")).toBeNull();
  });

  it("says nothing about a short positive ID, the shape of a personal chat", () => {
    expect(telegramChatIdWarning("123456789")).toBeNull();
  });

  it("says nothing about an empty or non-numeric value: a different problem, not this one", () => {
    expect(telegramChatIdWarning("")).toBeNull();
    expect(telegramChatIdWarning("not-a-number")).toBeNull();
  });

  it("warns about a 13+ digit positive number: a group ID typed without its minus sign", () => {
    expect(telegramChatIdWarning("1001234567890")).toBe(
      "This looks like a group's chat ID without its minus sign. Groups and supergroups use a negative ID (a supergroup's starts with -100); try -1001234567890.",
    );
  });

  it("ignores surrounding whitespace", () => {
    expect(telegramChatIdWarning("  1001234567890  ")).not.toBeNull();
  });
});

describe("the SMTP form", () => {
  const STORED = {
    host: "smtp.example.com",
    port: 587,
    use_ssl: false,
    use_tls: true,
    username: "wasm@example.com",
    from_address: "",
    recipients: ["ops@example.com"],
    password_set: true,
  };

  it("reads the two TLS switches as one choice, and never holds the password", () => {
    const form = smtpFormFrom(STORED);
    expect(form).toEqual({
      host: "smtp.example.com",
      port: "587",
      security: "starttls",
      username: "wasm@example.com",
      password: "",
      from_address: "",
      recipients: ["ops@example.com"],
    });
    expect(smtpFormFrom({ ...STORED, use_ssl: true, use_tls: false }).security).toBe("ssl");
    expect(smtpFormFrom({ ...STORED, use_ssl: false, use_tls: false }).security).toBe("none");
  });

  it("is dirty for a typed password or any other change, not for an empty password", () => {
    const stored = smtpFormFrom(STORED);
    expect(smtpFormDirty(stored, stored)).toBe(false);
    expect(smtpFormDirty({ ...stored, password: "x" }, stored)).toBe(true);
    expect(smtpFormDirty({ ...stored, port: " 587 " }, stored)).toBe(false);
    expect(smtpFormDirty({ ...stored, recipients: ["ops@example.com", "dev@example.com"] }, stored)).toBe(true);
    expect(smtpFormDirty({ ...stored, security: "ssl" }, stored)).toBe(true);
  });

  it("writes the choice back as the two switches, a port as a number, and anything else as typed", () => {
    const form = { ...smtpFormFrom(STORED), host: " smtp.example.com ", security: "ssl" as const, port: "465" };
    expect(smtpBody(form)).toEqual({
      host: "smtp.example.com",
      port: 465,
      use_ssl: true,
      use_tls: false,
      username: "wasm@example.com",
      password: "",
      from_address: "",
      recipients: ["ops@example.com"],
    });
    // The server rules on what is not a port; the console does not invent a message for it.
    expect(smtpBody({ ...form, port: "abc" }).port).toBe("abc");
  });

  it("moves the port with the choice only while it is the old choice's usual one", () => {
    expect(portForSecurity("465", "ssl", "starttls")).toBe("587");
    expect(portForSecurity("587", "starttls", "none")).toBe("25");
    expect(portForSecurity("2525", "ssl", "starttls")).toBe("2525");
  });

  it("checks an address's shape as wasm.core.config does, and splits a pasted list", () => {
    expect(looksLikeEmail("ops@example.com")).toBe(true);
    expect(looksLikeEmail("ops@example")).toBe(false);
    expect(looksLikeEmail("o ps@example.com")).toBe(false);
    expect(splitAddresses(" a@x.io, b@y.io;c@z.io  d@w.io ")).toEqual(["a@x.io", "b@y.io", "c@z.io", "d@w.io"]);
  });

  it("finds the addresses the server named in its refusal of the list", () => {
    const message = "monitor.email_recipients contains an invalid email address: a@b, c@d Every recipient must be an address such as ops@example.com.";
    expect([...refusedRecipients(message, ["a@b", "ops@example.com", "c@d"])]).toEqual(["a@b", "c@d"]);
    expect(refusedRecipients(undefined, ["a@b"]).size).toBe(0);
    expect(refusedRecipients("monitor.smtp.host is not a valid hostname: a@b", ["a@b"]).size).toBe(0);
  });
});

describe("Telegram chats", () => {
  it("names a chat by its title, else its username, else its type", () => {
    expect(telegramChatName({ id: -100, type: "supergroup", title: "Alerts", username: "alerts" })).toBe("Alerts");
    expect(telegramChatName({ id: 5, type: "private", title: null, username: "yago" })).toBe("@yago");
    expect(telegramChatName({ id: 5, type: "private", title: null, username: null })).toBe("Private chat");
    expect(telegramChatType("channel")).toBe("Channel");
    expect(telegramChatType("something_new")).toBe("something_new");
  });
});
