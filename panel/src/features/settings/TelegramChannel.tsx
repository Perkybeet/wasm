import { useMutation } from "@tanstack/react-query";
import { Check, Search, TriangleAlert } from "lucide-react";
import { useId, useRef, useState } from "react";
import type { SyntheticEvent } from "react";

import { announce } from "../../app/Announcer";
import { findTelegramChats, patchConfig, saveTelegramSettings } from "../../api/queries/config";
import type { TelegramChat } from "../../api/queries/config";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { toast } from "../../components/ui/toast";
import { ChannelHeader, DirtyActions, SecretInput, TestButton, TestOutcome, useChannelTest, useRefreshConfig } from "./channelParts";
import { splitConfigErrors } from "./formErrors";
import { CHANNELS, REDACTED, channelValue, telegramChatIdWarning, telegramChatName, telegramChatType } from "./notifications";

const SPEC = CHANNELS.find((spec) => spec.id === "telegram") ?? CHANNELS[0];
const TOKEN_FIELD = SPEC?.fields.find((field) => field.key === "bot_token");
const CHAT_FIELD = SPEC?.fields.find((field) => field.key === "chat_id");

type TelegramField = "bot_token" | "chat_id";
const FIELDS: readonly TelegramField[] = ["bot_token", "chat_id"];
const CONFIG_KEYS: Readonly<Record<string, TelegramField>> = {
  "notifications.channels.telegram.bot_token": "bot_token",
  "notifications.channels.telegram.chat_id": "chat_id",
};

/**
 * The chats the saved bot has seen, each one a click away from being the chat ID. Telegram only
 * knows a chat once someone has written in it with the bot there, so an empty answer explains
 * how to make one appear instead of just saying "none".
 */
function ChatFinder({
  tokenSaved,
  tokenTyped,
  chatId,
  onPick,
}: {
  tokenSaved: boolean;
  tokenTyped: boolean;
  chatId: string;
  onPick: (chat: TelegramChat) => void;
}) {
  const reasonId = useId();
  const find = useMutation({ mutationFn: findTelegramChats });
  const reason = !tokenSaved && !tokenTyped ? "Save the bot token first." : tokenTyped ? "Save the new token first." : undefined;
  const chats = find.data;

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Button
          size="sm"
          icon={<Search aria-hidden="true" />}
          loading={find.isPending}
          disabled={reason !== undefined}
          {...(reason !== undefined ? { "aria-describedby": reasonId } : {})}
          onClick={() => {
            find.mutate();
          }}
        >
          Find my chat
        </Button>
        <span id={reasonId} className="text-12 text-fg-faint">
          {reason ?? "Lists the chats your bot has seen, to pick the chat ID from."}
        </span>
      </div>
      {/* The count is announced; the list itself is read on demand. */}
      <p role="status" className="sr-only">
        {chats === undefined ? "" : chats.length === 0 ? "Your bot has not seen any chat yet." : `Found ${String(chats.length)} ${chats.length === 1 ? "chat" : "chats"}.`}
      </p>
      {find.isError ? <ErrorBlock compact error={find.error} title="Could not list the chats" /> : null}
      {chats?.length === 0 ? (
        <div className="flex flex-col gap-1 rounded-control border border-border bg-bg-sunken px-3 py-2 text-13 text-fg-muted">
          <p className="font-medium text-fg">Your bot has not seen any chat yet.</p>
          <p className="text-pretty">
            Add the bot to the group and send{" "}
            <code translate="no" className="mono rounded-[4px] bg-surface px-1 text-12 text-fg">
              /start@your_bot_name
            </code>{" "}
            there, with your bot's own username - or send it any message in a private chat. Then find your chat again.
          </p>
        </div>
      ) : null}
      {chats !== undefined && chats.length > 0 ? (
        <ul aria-label="Chats your bot has seen" className="flex min-w-0 flex-col divide-y divide-border rounded-control border border-border bg-bg-sunken">
          {chats.map((chat) => {
            const name = telegramChatName(chat);
            const chosen = chatId.trim() === String(chat.id);
            return (
              <li key={chat.id} className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 py-1.5 pr-1.5 pl-3">
                <div className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate text-13 text-fg" translate="no">
                    {name}
                  </span>
                  <span className="text-12 text-fg-muted">
                    {telegramChatType(chat.type)}
                    {chat.title && chat.username ? <span translate="no">{` · @${chat.username}`}</span> : null}
                  </span>
                </div>
                <span translate="no" className="mono shrink-0 text-12 text-fg-muted">
                  {chat.id}
                </span>
                {chosen ? (
                  <span className="flex h-7 shrink-0 items-center gap-1.5 px-2 text-12 text-fg">
                    <Check aria-hidden="true" className="size-3.5 text-ok" />
                    Chosen
                  </span>
                ) : (
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Use ${name}`}
                    onClick={() => {
                      onPick(chat);
                    }}
                  >
                    Use
                  </Button>
                )}
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}

/**
 * Telegram: a bot token and the chat it posts to. Saved through PUT
 * /api/config/notifications/telegram, which refuses a chat ID Telegram would refuse and names the
 * field; an empty token keeps the stored one. Removing it blanks the token through the generic
 * PATCH, which is the one write that can.
 */
export function TelegramChannel({ stored }: { stored: Readonly<Record<string, string>> }) {
  const refresh = useRefreshConfig();
  const headingId = useId();
  const [draft, setDraft] = useState<Partial<Record<TelegramField, string>>>({});
  const [edited, setEdited] = useState<ReadonlySet<TelegramField>>(new Set());
  // The chat ID the "missing minus" warning was last announced for, cleared by an edit. The
  // warning is drawn as the ID is typed (so it never moves the Save button under a click that
  // leaves the field) but is not a live region: its text holds the ID, so a live region would
  // be read out again on every digit. It is announced once, when the field is left with it.
  const announcedFor = useRef<string | null>(null);
  const test = useChannelTest("telegram");

  const tokenSaved = stored["bot_token"] === REDACTED;
  const typedToken = (draft.bot_token ?? "").trim();
  const chatId = draft.chat_id ?? stored["chat_id"] ?? "";
  const dirty = typedToken !== "" || (draft.chat_id !== undefined && draft.chat_id.trim() !== (stored["chat_id"] ?? ""));
  const configured = tokenSaved;

  const save = useMutation({
    mutationFn: () => saveTelegramSettings({ bot_token: typedToken, chat_id: chatId.trim() }),
    onSuccess: async () => {
      setDraft({});
      test.reset();
      await refresh();
      toast.success("Saved the Telegram destination");
    },
    onSettled: () => {
      setEdited(new Set());
    },
  });
  const remove = useMutation({
    mutationFn: () => {
      if (SPEC === undefined) return Promise.resolve(undefined);
      return patchConfig("notifications.channels.telegram", channelValue(SPEC, stored, {}, new Set(["bot_token"])));
    },
    onSuccess: async () => {
      setDraft({});
      test.reset();
      save.reset();
      await refresh();
      toast.success("Removed the Telegram destination");
    },
  });
  const pending = save.isPending || remove.isPending;
  const split = splitConfigErrors(save.error, FIELDS, CONFIG_KEYS);
  const errorOf = (name: TelegramField): string | undefined => (edited.has(name) ? undefined : split.fields[name]);

  const set = (name: TelegramField, value: string): void => {
    setDraft((current) => ({ ...current, [name]: value }));
    setEdited((current) => new Set([...current, name]));
  };

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (dirty && !pending) save.mutate();
  };

  const testReason = !configured ? "Add a destination to test it." : dirty ? "Save your changes first." : undefined;
  const warning = telegramChatIdWarning(chatId);

  return (
    <article aria-labelledby={headingId} className="flex min-w-0 flex-col gap-3 px-5 py-4">
      <ChannelHeader
        id={headingId}
        label="Telegram"
        description={SPEC?.description ?? ""}
        configured={configured}
        actions={
          <>
            {configured || remove.isPending ? (
              <Button
                size="sm"
                variant="ghost"
                disabled={save.isPending}
                loading={remove.isPending}
                onClick={() => {
                  remove.mutate();
                }}
              >
                Remove destination
              </Button>
            ) : null}
            <TestButton test={test} disabled={dirty || !configured} reason={testReason} />
          </>
        }
      />
      <form noValidate onSubmit={submit} className="flex min-w-0 flex-col gap-3">
        {split.form !== null ? <ErrorBlock live compact error={split.form} title="Could not save Telegram" /> : null}
        {remove.isError ? <ErrorBlock live compact error={remove.error} title="Could not remove the Telegram destination" /> : null}
        <div className="grid min-w-0 gap-3 sm:grid-cols-2">
          <Field label={TOKEN_FIELD?.label ?? "Bot token"} error={errorOf("bot_token")}>
            <SecretInput
              label={TOKEN_FIELD?.label ?? "Bot token"}
              placeholder={TOKEN_FIELD?.placeholder ?? ""}
              value={draft.bot_token ?? ""}
              configured={tokenSaved}
              disabled={pending}
              onChange={(next) => {
                set("bot_token", next);
              }}
            />
          </Field>
          <Field label={CHAT_FIELD?.label ?? "Chat ID"} description={CHAT_FIELD?.description} error={errorOf("chat_id")}>
            <Input
              mono
              autoComplete="off"
              spellCheck={false}
              placeholder={CHAT_FIELD?.placeholder}
              value={chatId}
              disabled={pending}
              onValueChange={(next: string) => {
                announcedFor.current = null;
                set("chat_id", next);
              }}
              onBlur={() => {
                if (warning === null || announcedFor.current === chatId) return;
                announcedFor.current = chatId;
                announce(warning);
              }}
            />
            {warning !== null ? (
              <p className="flex items-start gap-1.5 text-13 text-warn">
                <TriangleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
                <span>{warning}</span>
              </p>
            ) : null}
          </Field>
        </div>
        <ChatFinder
          tokenSaved={tokenSaved}
          tokenTyped={typedToken !== ""}
          chatId={chatId}
          onPick={(chat) => {
            set("chat_id", String(chat.id));
          }}
        />
        <DirtyActions
          dirty={dirty}
          pending={save.isPending}
          onDiscard={() => {
            setDraft({});
            setEdited(new Set());
            save.reset();
          }}
          note="A test goes to what is saved."
        />
      </form>
      <div role="status" className="min-w-0 empty:hidden">
        <TestOutcome result={test.data} error={test.error} source="Telegram" />
      </div>
    </article>
  );
}
