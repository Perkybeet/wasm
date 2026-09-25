/**
 * The `notifications` block of the configuration, read into something a form can hold.
 *
 * GET /api/config answers the whole file as an untyped tree with every secret replaced by
 * "***" - empty ones included, so the answer never says whether a webhook URL is set. The
 * console follows the same rule back: a secret field left untouched is sent as "***", which
 * the server resolves to the stored value (wasm.core.config.restore_redacted).
 */

import type { ConsoleConfig } from "../../api/queries/config";

/** What the server sends in place of a secret, and accepts back as "keep the stored one". */
export const REDACTED = "***";

export type ChannelId = "webhook" | "slack" | "discord" | "telegram" | "email";

export interface ChannelField {
  /** The key under `notifications.channels.<channel>`. */
  key: string;
  label: string;
  /** Secrets are write-only: masked while typed and never read back. */
  secret: boolean;
  placeholder: string;
}

export interface ChannelSpec {
  id: ChannelId;
  label: string;
  description: string;
  fields: readonly ChannelField[];
}

/** In the notifier's delivery order (wasm.core.notifier.CHANNELS). */
export const CHANNELS: readonly ChannelSpec[] = [
  {
    id: "webhook",
    label: "Webhook",
    description: "Your own endpoint receives a JSON POST with the event, a title, a body, the domain and the time.",
    fields: [{ key: "webhook_url", label: "Endpoint URL", secret: true, placeholder: "https://hooks.example.com/wasm" }],
  },
  {
    id: "slack",
    label: "Slack",
    description: "Posts to a channel through a Slack incoming webhook.",
    fields: [{ key: "webhook_url", label: "Incoming webhook URL", secret: true, placeholder: "https://hooks.slack.com/services/..." }],
  },
  {
    id: "discord",
    label: "Discord",
    description: "Posts to a channel through a Discord webhook.",
    fields: [{ key: "webhook_url", label: "Webhook URL", secret: true, placeholder: "https://discord.com/api/webhooks/..." }],
  },
  {
    id: "telegram",
    label: "Telegram",
    description: "Your bot sends the message to one chat.",
    fields: [
      { key: "bot_token", label: "Bot token", secret: true, placeholder: "123456789:AAH..." },
      { key: "chat_id", label: "Chat ID", secret: false, placeholder: "-1001234567890" },
    ],
  },
  {
    id: "email",
    label: "Email",
    description: "Sends through the monitor's SMTP account to its recipients.",
    fields: [],
  },
];

export interface EventSpec {
  kind: string;
  label: string;
  description: string;
  /** Set when this version of WASM never sends the event, so the switch changes nothing yet. */
  unsent?: true;
}

/** In the notifier's order (wasm.core.notifier.EVENT_KINDS), described by who sends them. */
export const EVENTS: readonly EventSpec[] = [
  { kind: "deploy_success", label: "Deploy finished", description: "A deploy, update or rollback completed." },
  { kind: "deploy_failed", label: "Deploy failed", description: "A deploy, update or rollback failed, with the tool's own error." },
  {
    kind: "cert_expiring",
    label: "Certificate expiring",
    description: "A certificate is close to its expiry date.",
    unsent: true,
  },
  { kind: "unit_failed", label: "Service down", description: "A watched service stopped running. Sent by the monitor." },
  { kind: "disk_threshold", label: "Disk almost full", description: "A disk passed 90% usage. Sent by the monitor." },
  { kind: "backup_failed", label: "Backup failed", description: "A backup job failed." },
];

export interface SmtpFacts {
  host: string;
  port: number | null;
  from: string;
  recipients: readonly string[];
}

export interface NotificationSettings {
  enabled: boolean;
  events: Record<string, boolean>;
  /** Field values per channel; secrets arrive as "***". */
  channels: Record<ChannelId, Record<string, string>>;
  emailEnabled: boolean;
  allowPrivateHosts: readonly string[];
  smtp: SmtpFacts;
}

type Tree = Record<string, unknown>;

function isTree(value: unknown): value is Tree {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function branch(tree: unknown, key: string): Tree {
  if (!isTree(tree)) return {};
  const value = tree[key];
  return isTree(value) ? value : {};
}

function text(tree: Tree, key: string): string {
  const value = tree[key];
  return typeof value === "string" ? value : typeof value === "number" ? String(value) : "";
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

/** Reads the notification settings, and the SMTP account email goes through, out of the configuration. */
export function readNotificationSettings(config: ConsoleConfig["config"]): NotificationSettings {
  const block = branch(config, "notifications");
  const channelsBlock = branch(block, "channels");
  const eventsBlock = branch(block, "events");
  const monitor = branch(config, "monitor");
  const smtp = branch(monitor, "smtp");

  const channels = {} as Record<ChannelId, Record<string, string>>;
  for (const spec of CHANNELS) {
    const stored = branch(channelsBlock, spec.id);
    channels[spec.id] = Object.fromEntries(spec.fields.map((field) => [field.key, text(stored, field.key)]));
  }
  const events: Record<string, boolean> = {};
  for (const spec of EVENTS) {
    // The notifier treats an event missing from the file as on (events.get(kind, True)).
    events[spec.kind] = eventsBlock[spec.kind] !== false;
  }
  const port = smtp["port"];
  return {
    enabled: block["enabled"] === true,
    events,
    channels,
    emailEnabled: branch(channelsBlock, "email")["enabled"] === true,
    allowPrivateHosts: strings(block["allow_private_hosts"]),
    smtp: {
      host: text(smtp, "host"),
      port: typeof port === "number" ? port : null,
      from: text(smtp, "from_address"),
      recipients: strings(monitor["email_recipients"]),
    },
  };
}

/**
 * The value to write at `notifications.channels.<channel>` for a form's fields: what was typed,
 * or "***" for a secret left empty so the server keeps the stored one.
 *
 * @param cleared Secret fields the operator chose to remove: written as "", which turns the
 *   channel off.
 */
export function channelValue(
  spec: ChannelSpec,
  draft: Readonly<Record<string, string>>,
  cleared: ReadonlySet<string> = new Set(),
): Record<string, string> {
  return Object.fromEntries(
    spec.fields.map((field) => {
      const typed = (draft[field.key] ?? "").trim();
      if (cleared.has(field.key)) return [field.key, ""];
      if (field.secret) return [field.key, typed === "" ? REDACTED : typed];
      return [field.key, typed];
    }),
  );
}

/** Private hosts as typed, one per line or separated by commas, without blanks or repeats. */
export function parseHostList(value: string): string[] {
  const hosts = value
    .split(/[\s,]+/)
    .map((host) => host.trim().toLowerCase())
    .filter((host) => host !== "");
  return [...new Set(hosts)];
}
