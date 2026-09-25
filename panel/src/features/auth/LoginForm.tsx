import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { ArrowLeft, Info } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { SyntheticEvent } from "react";

import { isApiError } from "../../api/client";
import { login, sessionQuery } from "../../api/queries/auth";
import { announce } from "../../app/Announcer";
import { Button } from "../../components/ui/Button";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { describeError } from "../../lib/errors";
import type { DescribedError } from "../../lib/errors";

type Step = "token" | "code";

/** Seconds left of a lockout, counting down to zero once started. */
function useCountdown(): [number, (seconds: number) => void] {
  const [remaining, setRemaining] = useState(0);
  useEffect(() => {
    if (remaining <= 0) return;
    const timer = setTimeout(() => {
      setRemaining((seconds) => seconds - 1);
    }, 1_000);
    return () => {
      clearTimeout(timer);
    };
  }, [remaining]);
  return [remaining, setRemaining];
}

function formatWait(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${String(minutes)}:${String(rest).padStart(2, "0")}`;
}

export interface LoginFormProps {
  /** Where to go once signed in; already checked to be a path of this console. */
  next: string;
  /** The operator was sent here because their session ended. */
  expired: boolean;
}

/**
 * Sign-in: the access token, then a second step for the two-factor or backup code when the
 * server asks for one. A lockout shows how long is left; errors show the server's words.
 */
export function LoginForm({ next, expired }: LoginFormProps) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { data: session, error: sessionError } = useQuery(sessionQuery());

  const [step, setStep] = useState<Step>("token");
  const [token, setToken] = useState("");
  const [code, setCode] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [failure, setFailure] = useState<DescribedError | null>(null);
  const [pending, setPending] = useState(false);

  const tokenRef = useRef<HTMLInputElement>(null);
  const codeRef = useRef<HTMLInputElement>(null);

  const [remaining, lockFor] = useCountdown();
  const locked = remaining > 0;

  useEffect(() => {
    (step === "token" ? tokenRef : codeRef).current?.focus();
  }, [step]);

  // A refused value is selected for retyping once the field is enabled again: the fields are
  // disabled while a request is in flight, and a disabled field drops focus and ignores
  // select(), which left keyboard and screen reader users on <body> after every typo.
  useEffect(() => {
    if (pending || fieldError === null) return;
    const field = (step === "token" ? tokenRef : codeRef).current;
    field?.focus();
    field?.select();
  }, [fieldError, pending, step]);

  useEffect(() => {
    if (expired) announce("Your session expired. Sign in again to continue where you left off.");
  }, [expired]);

  const backToToken = (): void => {
    setStep("token");
    setCode("");
    setFieldError(null);
    setFailure(null);
  };

  const submit = async (event: SyntheticEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (pending || locked) return;
    const value = step === "token" ? token : code.trim();
    if (value === "") {
      setFieldError(step === "token" ? "Enter the access token." : "Enter the code.");
      return;
    }
    setPending(true);
    setFieldError(null);
    setFailure(null);
    try {
      // bearer: false - the browser keeps the session in an HttpOnly cookie and never sees it.
      await login(step === "token" ? { token, bearer: false } : { token, bearer: false, totp_code: code.trim() });
      await queryClient.query({ ...sessionQuery(), staleTime: 0 });
      announce("Signed in");
      await navigate({ href: next, replace: true });
    } catch (error: unknown) {
      if (!isApiError(error)) {
        setFailure(describeError(error));
        return;
      }
      switch (error.error) {
        case "totp_required":
          setStep("code");
          announce("Token accepted. Enter your two-factor code.");
          return;
        case "invalid_token":
          backToToken();
          setFieldError(error.detail);
          return;
        case "invalid_totp":
          setFieldError(error.detail);
          return;
        case "locked_out":
        case "rate_limited": {
          const seconds = error.retryAfter ?? 60;
          lockFor(seconds);
          announce(`${error.detail} Try again in ${formatWait(seconds)}.`, "assertive");
          return;
        }
        default:
          setFailure(describeError(error));
      }
    } finally {
      setPending(false);
    }
  };

  const unreachable = sessionError !== null ? describeError(sessionError) : null;
  const shownFailure = failure ?? unreachable;

  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex flex-col gap-5">
      {expired ? (
        <div className="flex items-start gap-2.5 rounded-control border border-border bg-surface px-3 py-2.5 text-13 text-fg shadow-raised">
          <Info aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-fg-muted" />
          <p>Your session expired. Sign in again to continue where you left off.</p>
        </div>
      ) : null}

      {locked ? (
        <div className="flex flex-col gap-0.5 rounded-control border border-fail/30 bg-fail-soft px-3 py-2.5 text-13">
          <p className="font-medium text-fail">Too many failed attempts</p>
          <p className="text-fg">
            Sign-in is locked for this address. Try again in{" "}
            <span className="mono font-medium" aria-hidden="true">
              {formatWait(remaining)}
            </span>
            <span className="sr-only">{`${String(Math.ceil(remaining / 60))} minutes`}</span>.
          </p>
        </div>
      ) : null}

      {shownFailure !== null ? (
        <div role="alert" className="flex flex-col gap-2 rounded-control border border-fail/30 bg-fail-soft p-3">
          <p className="text-13 font-medium text-fail">{shownFailure.hint ?? "Signing in failed. The server said:"}</p>
          <pre className="max-h-40 overflow-auto text-12 whitespace-pre-wrap text-fg scroll-thin">{shownFailure.detail}</pre>
        </div>
      ) : null}

      {/* Password managers file a secret under a user name; the machine is the natural one. */}
      <input type="text" name="username" autoComplete="username" value={session?.hostname ?? ""} readOnly hidden />

      {step === "token" ? (
        <Field
          label="Access token"
          error={fieldError}
          description={
            <>
              Print it on the server with{" "}
              <code className="mono rounded-[4px] bg-bg-sunken px-1 py-0.5 text-fg">wasm web token</code>
            </>
          }
        >
          <Input
            ref={tokenRef}
            name="token"
            type="password"
            mono
            autoComplete="current-password"
            autoCapitalize="off"
            spellCheck={false}
            value={token}
            onValueChange={(value: string) => {
              setToken(value);
            }}
            disabled={pending}
          />
        </Field>
      ) : (
        <>
          <div className="flex items-center justify-between gap-3 rounded-control border border-border bg-bg-sunken py-1.5 pr-1.5 pl-3">
            <span className="text-13 text-fg-muted">
              Access token <span className="text-fg">accepted</span>
            </span>
            <Button variant="ghost" size="sm" icon={<ArrowLeft aria-hidden="true" />} onClick={backToToken} disabled={pending}>
              Use a different token
            </Button>
          </div>
          <Field
            label="Two-factor code"
            error={fieldError}
            description="The 6-digit code from your authenticator app, or one of your backup codes."
          >
            <Input
              ref={codeRef}
              name="totp_code"
              mono
              autoComplete="one-time-code"
              autoCapitalize="off"
              spellCheck={false}
              maxLength={32}
              value={code}
              onValueChange={(value: string) => {
                setCode(value);
              }}
              disabled={pending}
            />
          </Field>
        </>
      )}

      <Button type="submit" variant="primary" size="lg" loading={pending} disabled={locked} className="w-full">
        {step === "token" ? "Sign in" : "Verify"}
      </Button>
    </form>
  );
}
