import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useRef, useState } from "react";
import type { SyntheticEvent } from "react";

import { isApiError } from "../../api/client";
import { authKeys, elevateSession, sessionQuery } from "../../api/queries/auth";
import type { SessionInfo } from "../../api/queries/auth";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { SystemOutput } from "../../components/ui/SystemOutput";
import { describeError } from "../../lib/errors";
import type { DescribedError } from "../../lib/errors";
import { cancelElevation, resolveElevation, useElevationRequested } from "./elevation";

export { elevate } from "./elevation";

/**
 * "Confirm it's you": the sudo-mode prompt for destructive actions (D5). Mounted once, at the
 * root; opened by the API client when an action answers 403 elevation_required, which then
 * retries the action. The factor is the one a sign-in asks for: a two-factor or backup code
 * when two-factor authentication is on, the access token when it is off.
 */
export function ElevateDialog() {
  const open = useElevationRequested();
  const queryClient = useQueryClient();
  const { data: session } = useQuery({ ...sessionQuery(), enabled: open });
  const totp = session?.totp_enabled ?? false;

  const [value, setValue] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [failure, setFailure] = useState<DescribedError | null>(null);
  const [pending, setPending] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const formId = useId();

  const reset = (): void => {
    setValue("");
    setFieldError(null);
    setFailure(null);
  };

  const onOpenChange = (next: boolean): void => {
    if (next || pending) return;
    reset();
    cancelElevation();
  };

  const submit = async (event: SyntheticEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (pending || value.trim() === "") return;
    setPending(true);
    setFieldError(null);
    setFailure(null);
    try {
      const { elevated_until } = await elevateSession(totp ? { code: value.trim() } : { token: value.trim() });
      queryClient.setQueryData(authKeys.session, (current: SessionInfo | undefined) =>
        current ? { ...current, elevated_until } : current,
      );
      reset();
      resolveElevation();
    } catch (error: unknown) {
      if (isApiError(error) && error.sessionExpired) {
        // The client is already on its way to the sign-in page, which cancels this request.
        return;
      }
      if (isApiError(error) && (error.status === 401 || error.error === "locked_out")) {
        setFieldError(error.detail);
      } else {
        setFailure(describeError(error));
      }
      inputRef.current?.select();
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="sm"
      initialFocus={inputRef}
      title="Confirm it's you"
      description={
        totp
          ? "This action needs a recent confirmation. Enter a code from your authenticator app or one of your backup codes. It covers the next 10 minutes."
          : "This action needs a recent confirmation. Enter the access token of this server. It covers the next 10 minutes."
      }
      footer={
        <>
          <Button
            disabled={pending}
            onClick={() => {
              onOpenChange(false);
            }}
          >
            Cancel
          </Button>
          <Button type="submit" form={formId} variant="primary" loading={pending} disabled={value.trim() === ""}>
            Confirm
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={(event) => void submit(event)} className="flex flex-col gap-4">
        <Field label={totp ? "Authentication code" : "Access token"} error={fieldError}>
          <Input
            ref={inputRef}
            mono
            {...(totp ? { autoComplete: "one-time-code", type: "text" } : { autoComplete: "current-password", type: "password" })}
            autoCapitalize="off"
            spellCheck={false}
            value={value}
            onValueChange={(next: string) => {
              setValue(next);
            }}
            disabled={pending}
          />
        </Field>
        {failure !== null ? (
          <div role="alert" className="flex flex-col gap-2 rounded-control border border-fail/30 bg-fail-soft p-3">
            <p className="text-13 font-medium text-fail">{failure.hint ?? "The confirmation failed. The system said:"}</p>
            <SystemOutput label="What the system said" maxHeight="max-h-40">
              {failure.detail}
            </SystemOutput>
          </div>
        ) : null}
      </form>
    </Dialog>
  );
}
