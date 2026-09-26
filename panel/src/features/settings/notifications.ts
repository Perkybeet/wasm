/**
 * The `notifications` block of the configuration, read into something a form can hold.
 *
 * GET /api/config answers the whole file as an untyped tree with every secret replaced by
 * "" when nothing is stored and "***" when something is (wasm.core.config.redact_secrets) -
 * so the answer says whether a channel has a destination without ever showing it. The console
 * follows the same rule back: a secret field left untouched is sent as "***", which the server
 * resolves to the stored value (wasm.core.config.restore_redacted); a field the operator never
 * touched is otherwise sent back exactly as it was read, so saving one field never blanks
 * another it shares a channel with.
 */

import type { ConsoleConfig, SmtpBody, SmtpSettings, TelegramChat } from "../../api/queries/config";

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
  /** Help shown under the field regardless of what is typed: format, consequence, default. */
  description?: string;
  /** A non-blocking warning shown under the field when what is typed looks like a mistake. */
  warn?: (value: string) => string | null;
}

/**
 * Telegram gives a bot's own chat a small positive ID, but a group or supergroup one that is
 * negative (a supergroup's starts with -100). Typing the group's number without its minus sign
 * is the one mistake that looks valid (it is still digits) and sends nothing, silently, because
 * the bot API answers "chat not found" for an ID that belongs to no chat it can reach - so this
 * warns before saving instead of after the first message never arrives. 13+ digits is a
 * supergroup's own id range with the sign stripped; a personal chat's id is far shorter.
 */
export function telegramChatIdWarning(value: string): string | null {
  const trimmed = value.trim();
  if (!/^\d{13,}$/.test(trimmed)) return null;
  return `This looks like a group's chat ID without its minus sign. Groups and supergroups use a negative ID (a supergroup's starts with -100); try -${trimmed}.`;
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
      {
        key: "chat_id",
        label: "Chat ID",
        secret: false,
        placeholder: "-1001234567890",
        description: "A group or supergroup's chat ID is negative, and a supergroup's starts with -100. A personal chat's is a smaller positive number.",
        warn: telegramChatIdWarning,
      },
    ],
  },
  {
    id: "email",
    label: "Email",
    description: "Sent through your SMTP server to the recipients below. The resource monitor's own reports use the same account.",
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

/** Whether a channel has a destination: one of its secret fields is a stored "***". */
export function isChannelConfigured(spec: ChannelSpec, stored: Readonly<Record<string, string>>): boolean {
  return spec.fields.some((field) => field.secret && stored[field.key] === REDACTED);
}

/**
 * The value to write at `notifications.channels.<channel>` for a form's fields: what the
 * operator typed, or, for whatever they left alone, exactly what is already stored - so saving
 * one field of a multi-field channel (Telegram's chat ID beside its bot token) never sends the
 * other back blank. A secret left untouched is sent as "***", which the server resolves to the
 * stored value (wasm.core.config.restore_redacted) instead of the literal three characters.
 *
 * @param stored What the channel holds now, as `readNotificationSettings` read it: secrets
 *   already redacted, everything else in clear.
 * @param cleared Secret fields the operator chose to remove: written as "", which turns the
 *   channel off.
 */
export function channelValue(
  spec: ChannelSpec,
  stored: Readonly<Record<string, string>>,
  draft: Readonly<Record<string, string>>,
  cleared: ReadonlySet<string> = new Set(),
): Record<string, string> {
  return Object.fromEntries(
    spec.fields.map((field) => {
      if (cleared.has(field.key)) return [field.key, ""];
      if (field.secret) {
        const typed = (draft[field.key] ?? "").trim();
        return [field.key, typed === "" ? REDACTED : typed];
      }
      const typed = draft[field.key]?.trim();
      return [field.key, typed ?? stored[field.key] ?? ""];
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

// ---------------------------------------------------------------------------------------
// Email: the monitor's SMTP account (GET/PUT /api/config/smtp)

/**
 * How the connection to the SMTP server is secured. The configuration holds two switches,
 * `use_ssl` and `use_tls`, that must never both be on, so the form offers the three
 * combinations that describe a real connection as one choice.
 */
export type SmtpSecurity = "ssl" | "starttls" | "none";

export const SMTP_SECURITY: readonly { value: SmtpSecurity; label: string; port: number; description: string }[] = [
  { value: "ssl", label: "SSL/TLS", port: 465, description: "Encrypted from the first byte (implicit TLS), usually on port 465." },
  { value: "starttls", label: "STARTTLS", port: 587, description: "Connects in the clear and upgrades to TLS before anything is sent, usually on port 587." },
  {
    value: "none",
    label: "None",
    port: 25,
    description: "Unencrypted, for an anonymous relay on this machine or a private network. A username or password is never sent this way.",
  },
];

export interface SmtpForm {
  host: string;
  /** As typed: the server rules on whether it is a port. */
  port: string;
  security: SmtpSecurity;
  username: string;
  /** Write-only: empty keeps the stored password. */
  password: string;
  from_address: string;
  recipients: readonly string[];
}

export type SmtpField = "host" | "port" | "security" | "username" | "password" | "from_address" | "recipients";

export const SMTP_FIELDS: readonly SmtpField[] = ["host", "port", "security", "username", "password", "from_address", "recipients"];

/**
 * The model field each configuration key is validated under (wasm.core.config._KEY_VALIDATORS),
 * so a refusal naming the key lands beside the field that holds it.
 */
export const SMTP_CONFIG_KEYS: Readonly<Record<string, SmtpField>> = {
  "monitor.smtp.host": "host",
  "monitor.smtp.port": "port",
  "monitor.smtp.use_ssl": "security",
  "monitor.smtp.use_tls": "security",
  "monitor.smtp.username": "username",
  "monitor.smtp.password": "password",
  "monitor.smtp.from_address": "from_address",
  "monitor.email_recipients": "recipients",
};

export function smtpSecurityOf(settings: Pick<SmtpSettings, "use_ssl" | "use_tls">): SmtpSecurity {
  if (settings.use_ssl) return "ssl";
  return settings.use_tls ? "starttls" : "none";
}

/** The form's starting values: what the server holds, with the password field empty. */
export function smtpFormFrom(settings: SmtpSettings): SmtpForm {
  return {
    host: settings.host,
    port: String(settings.port),
    security: smtpSecurityOf(settings),
    username: settings.username,
    password: "",
    from_address: settings.from_address,
    recipients: settings.recipients,
  };
}

/**
 * Whether a form differs from what is stored. A typed password is a change; an empty one keeps
 * the stored password, so it is not.
 */
export function smtpFormDirty(form: SmtpForm, stored: SmtpForm): boolean {
  return (
    form.host !== stored.host ||
    form.port.trim() !== stored.port ||
    form.security !== stored.security ||
    form.username !== stored.username ||
    form.password !== "" ||
    form.from_address !== stored.from_address ||
    form.recipients.length !== stored.recipients.length ||
    form.recipients.some((address, index) => address !== stored.recipients[index])
  );
}

/**
 * The PUT body for a form. The port goes as a number when it is one; anything else is sent as
 * typed, so the server's own message about it lands beside the field instead of the console
 * inventing one.
 */
export function smtpBody(form: SmtpForm): SmtpBody {
  const typedPort = form.port.trim();
  const port = /^\d+$/.test(typedPort) ? Number(typedPort) : (typedPort as unknown as number);
  return {
    host: form.host.trim(),
    port,
    use_ssl: form.security === "ssl",
    use_tls: form.security === "starttls",
    username: form.username.trim(),
    password: form.password,
    from_address: form.from_address.trim(),
    recipients: [...form.recipients],
  };
}

/**
 * When the security choice changes and the port is still the old choice's usual one, the new
 * choice's usual port; otherwise the port as it is, since the operator chose it on purpose.
 */
export function portForSecurity(port: string, from: SmtpSecurity, to: SmtpSecurity): string {
  const previous = SMTP_SECURITY.find((option) => option.value === from)?.port;
  const next = SMTP_SECURITY.find((option) => option.value === to)?.port;
  return next !== undefined && port.trim() === String(previous) ? String(next) : port;
}

/** The same shape `wasm.core.config._EMAIL_PATTERN` accepts: something@something.tld, no spaces. */
const EMAIL_SHAPE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export function looksLikeEmail(value: string): boolean {
  return EMAIL_SHAPE.test(value);
}

/** Addresses typed into the recipients box: one or several, separated by commas, semicolons or spaces. */
export function splitAddresses(value: string): string[] {
  return value
    .split(/[\s,;]+/)
    .map((address) => address.trim())
    .filter((address) => address !== "");
}

/**
 * The addresses a refusal of `monitor.email_recipients` names: the server lists the invalid
 * ones after the colon ("... contains an invalid email address: a, b").
 */
export function refusedRecipients(message: string | undefined, recipients: readonly string[]): ReadonlySet<string> {
  if (message === undefined) return new Set();
  const colon = message.indexOf(": ");
  if (!message.startsWith("monitor.email_recipients") || colon === -1) return new Set();
  // The hint follows the list after a sentence break; only exact addresses of the form count.
  const named = message.slice(colon + 2).split(/,\s*|\s+/);
  return new Set(recipients.filter((address) => named.includes(address)));
}

// ---------------------------------------------------------------------------------------
// Telegram: the chats the bot has seen

const CHAT_TYPES: Readonly<Record<string, string>> = {
  private: "Private chat",
  group: "Group",
  supergroup: "Supergroup",
  channel: "Channel",
};

/** A chat's type in words. */
export function telegramChatType(type: string): string {
  return CHAT_TYPES[type] ?? type;
}

/** What a chat is called: its title, else its @username, else its type. */
export function telegramChatName(chat: TelegramChat): string {
  if (chat.title) return chat.title;
  if (chat.username) return `@${chat.username}`;
  return telegramChatType(chat.type);
}
