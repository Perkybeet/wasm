import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { BodyOf, ResponseOf } from "../client";

/** The whole configuration, secrets redacted to "***", with its file path and writability. */
export type ConsoleConfig = ResponseOf<"/api/config", "get">;

export type AppsDirectorySettings = ResponseOf<"/api/config/apps-directory", "get">;
export type WebserverSettings = ResponseOf<"/api/config/webserver", "get">;
export type BackupSettings = ResponseOf<"/api/config/backup", "get">;
export type SslSettings = ResponseOf<"/api/config/ssl", "get">;
export type WebSettings = ResponseOf<"/api/config/web", "get">;

export type AppsDirectoryBody = BodyOf<"/api/config/apps-directory", "put">;
export type WebserverBody = BodyOf<"/api/config/webserver", "put">;
export type BackupBody = BodyOf<"/api/config/backup", "put">;
export type SslBody = BodyOf<"/api/config/ssl", "put">;
export type WebBody = BodyOf<"/api/config/web", "put">;

export type NotificationTestResult = ResponseOf<"/api/config/notifications/{channel}/test", "post">;

/** The monitor's SMTP account, which email notifications go through. The password never comes back. */
export type SmtpSettings = ResponseOf<"/api/config/smtp", "get">;
export type SmtpBody = BodyOf<"/api/config/smtp", "put">;
export type TelegramBody = BodyOf<"/api/config/notifications/telegram", "put">;
export type TelegramChat = ResponseOf<"/api/config/notifications/telegram/chats", "post">["chats"][number];

/** The typed sections, each one GET and one PUT of its own. */
export type ConfigSection = "apps-directory" | "webserver" | "backup" | "ssl" | "web" | "smtp";

export const configKeys = {
  all: ["config"] as const,
  defaults: ["config", "defaults"] as const,
  /** Under `all`, so a write that invalidates the configuration refreshes every section. */
  section: (name: ConfigSection) => ["config", "section", name] as const,
};

export const configQuery = () =>
  queryOptions({
    queryKey: configKeys.all,
    queryFn: ({ signal }) => request("get", "/api/config", { signal }),
  });

export const configDefaultsQuery = () =>
  queryOptions({
    queryKey: configKeys.defaults,
    queryFn: ({ signal }) => request("get", "/api/config/defaults", { signal }),
    staleTime: Number.POSITIVE_INFINITY,
  });

export const appsDirectoryQuery = () =>
  queryOptions({
    queryKey: configKeys.section("apps-directory"),
    queryFn: ({ signal }) => request("get", "/api/config/apps-directory", { signal }),
  });

export const webserverQuery = () =>
  queryOptions({
    queryKey: configKeys.section("webserver"),
    queryFn: ({ signal }) => request("get", "/api/config/webserver", { signal }),
  });

export const backupSettingsQuery = () =>
  queryOptions({
    queryKey: configKeys.section("backup"),
    queryFn: ({ signal }) => request("get", "/api/config/backup", { signal }),
  });

export const sslSettingsQuery = () =>
  queryOptions({
    queryKey: configKeys.section("ssl"),
    queryFn: ({ signal }) => request("get", "/api/config/ssl", { signal }),
  });

export const webSettingsQuery = () =>
  queryOptions({
    queryKey: configKeys.section("web"),
    queryFn: ({ signal }) => request("get", "/api/config/web", { signal }),
  });

export const smtpSettingsQuery = () =>
  queryOptions({
    queryKey: configKeys.section("smtp"),
    queryFn: ({ signal }) => request("get", "/api/config/smtp", { signal }),
  });

export function saveAppsDirectory(body: AppsDirectoryBody) {
  return request("put", "/api/config/apps-directory", { body });
}

export function saveWebserver(body: WebserverBody) {
  return request("put", "/api/config/webserver", { body });
}

export function saveBackupSettings(body: BackupBody) {
  return request("put", "/api/config/backup", { body });
}

export function saveSslSettings(body: SslBody) {
  return request("put", "/api/config/ssl", { body });
}

export function saveWebSettings(body: WebBody) {
  return request("put", "/api/config/web", { body });
}

/**
 * Writes one value addressed by its dotted key (`notifications.enabled`). A secret sent back
 * as "***" keeps the stored one. Needs a recent "Confirm it's you"; the client asks for it.
 */
export function patchConfig(path: string, value: unknown) {
  return request("patch", "/api/config", { body: { path, value } });
}

/** Sends a test message through one notification channel, as the configuration on disk stands. */
export function testNotificationChannel(channel: string) {
  return request("post", "/api/config/notifications/{channel}/test", { params: { channel } });
}

/**
 * Writes the SMTP account and its recipients. An empty password keeps the stored one. Needs a
 * recent "Confirm it's you"; the client asks for it.
 */
export function saveSmtpSettings(body: SmtpBody) {
  return request("put", "/api/config/smtp", { body });
}

/**
 * Writes the Telegram bot token and chat ID. An empty token keeps the stored one; a chat ID
 * Telegram would refuse is answered as a 422 naming `chat_id`.
 */
export function saveTelegramSettings(body: TelegramBody) {
  return request("put", "/api/config/notifications/telegram", { body });
}

/** The chats the saved Telegram bot has seen, read from the Bot API's queued updates. */
export async function findTelegramChats(): Promise<readonly TelegramChat[]> {
  const result = await request("post", "/api/config/notifications/telegram/chats");
  return result.chats;
}
