import { describe, expect, it } from "vitest";

import { CHANNELS, REDACTED, channelValue, isChannelConfigured, parseHostList, readNotificationSettings } from "./notifications";
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
