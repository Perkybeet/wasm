import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, ShieldCheck, ShieldOff, TriangleAlert } from "lucide-react";
import { useId, useRef, useState } from "react";
import type { RefObject, SyntheticEvent } from "react";

import {
  authKeys,
  confirmTwoFactor,
  disableTwoFactor,
  enrollTwoFactor,
  regenerateBackupCodes,
  sessionQuery,
  twoFactorQuery,
} from "../../api/queries/auth";
import type { TwoFactorEnrollment, TwoFactorStatus } from "../../api/queries/auth";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { Checkbox } from "../../components/ui/Checkbox";
import { CopyButton } from "../../components/ui/CopyButton";
import { CopyTextButton } from "../../components/ui/CopyTextButton";
import { Dialog } from "../../components/ui/Dialog";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { Skeleton } from "../../components/ui/Skeleton";
import { toast } from "../../components/ui/toast";
import { downloadText } from "../../lib/clipboard";
import { cx } from "../../lib/cx";
import { reportActionError } from "../apps/useAppActions";
import { splitErrors } from "./formErrors";
import { QrCode } from "./QrCode";
import { backupCodesFile, groupSecret } from "./security";
import { SettingsSection } from "./SettingsForm";

/** How many backup codes a confirmed enrolment issues (wasm.web.auth.BACKUP_CODE_COUNT). */
const BACKUP_CODES = 8;

/** Low enough that the operator should plan for new ones. */
const FEW_CODES = 2;

function useRefreshTwoFactor() {
  const queryClient = useQueryClient();
  return (): void => {
    void queryClient.invalidateQueries({ queryKey: authKeys.twoFactor });
    // The session answer carries totp_enabled, which decides what "Confirm it's you" asks for.
    void queryClient.invalidateQueries({ queryKey: authKeys.session });
  };
}

// ---------------------------------------------------------------------------------------
// Enrolment

export interface EnrollDialogProps {
  open: boolean;
  enrollment: TwoFactorEnrollment | null;
  onClose: () => void;
}

/**
 * Setting up two-factor authentication: scan the code (or type the key), prove it with a code
 * from the app, then keep the backup codes, which are shown this once. Keyed by the secret, so
 * every setup starts from a clean state; closing it for any reason clears that state again, so
 * the secret and the backup codes never outlive the dialog that showed them.
 *
 * Exported for `TwoFactorSection.test.tsx`, which checks that in isolation from the parent's
 * own key-based reset.
 */
export function EnrollDialog({ open, enrollment, onClose }: EnrollDialogProps) {
  const refresh = useRefreshTwoFactor();
  const { data: session } = useQuery(sessionQuery());
  const hostname = session?.hostname ?? "this server";
  const [codes, setCodes] = useState<readonly string[] | null>(null);
  const [code, setCode] = useState("");
  const [saved, setSaved] = useState(false);
  const [nudge, setNudge] = useState(false);
  const codeRef = useRef<HTMLInputElement>(null);
  const savedRef = useRef<HTMLDivElement>(null);
  const formId = useId();

  const confirm = useMutation({
    mutationFn: (value: string) => confirmTwoFactor(value),
    onSuccess: (result) => {
      setCodes(result.backup_codes);
      refresh();
    },
    onError: () => {
      codeRef.current?.select();
    },
  });
  const codeError = splitErrors(confirm.error, ["code"], "code");

  const onOpenChange = (next: boolean): void => {
    if (next || confirm.isPending) return;
    if (codes !== null && !saved) {
      // Closing now would lose the only copy of the codes: say so, and point at the box.
      setNudge(true);
      savedRef.current?.querySelector<HTMLElement>("[role=checkbox]")?.focus();
      return;
    }
    if (codes !== null) toast.success("Turned on two-factor authentication");
    // The secret, its QR URI and the backup codes are plaintext in state only while this
    // dialog is open; closing it - cancelled, confirmed, or dismissed however it closes -
    // wipes them, rather than leaving them sitting in memory for as long as this settings
    // page stays mounted.
    setCodes(null);
    setCode("");
    setSaved(false);
    setNudge(false);
    onClose();
  };

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    const value = code.replace(/\s+/g, "");
    if (value === "" || confirm.isPending) return;
    confirm.mutate(value);
  };

  if (codes !== null) {
    return (
      <BackupCodesDialog
        open={open}
        codes={codes}
        hostname={hostname}
        description="Two-factor authentication is on. Each backup code signs in once if your authenticator is lost. WASM keeps only their hashes, so this is the only time they are shown."
        saved={saved}
        nudge={nudge}
        savedRef={savedRef}
        onSavedChange={(next) => {
          setSaved(next);
          if (next) setNudge(false);
        }}
        onOpenChange={onOpenChange}
      />
    );
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      initialFocus={codeRef}
      title="Set up two-factor authentication"
      description="Sign-in and destructive actions will ask for a code from an authenticator app, such as 1Password, Google Authenticator or Aegis."
      footer={
        <>
          <Button
            disabled={confirm.isPending}
            onClick={() => {
              onOpenChange(false);
            }}
          >
            Cancel
          </Button>
          <Button type="submit" form={formId} variant="primary" loading={confirm.isPending} disabled={code.trim() === ""}>
            Turn on
          </Button>
        </>
      }
    >
      {enrollment !== null ? (
        <ol className="flex flex-col gap-6">
          <li className="flex flex-col gap-3">
            <p className="text-14 font-medium text-fg">1. Scan this code with the app</p>
            <div className="flex flex-col gap-5 sm:flex-row sm:items-start">
              <QrCode value={enrollment.uri} label={`QR code to add WASM (${hostname}) to an authenticator app`} />
              <div className="flex min-w-0 flex-col gap-3">
                <div className="flex min-w-0 flex-col gap-1">
                  <span className="text-13 text-fg-muted">Cannot scan it? Type this key instead.</span>
                  <div className="flex min-w-0 items-center gap-1 rounded-control border border-border bg-bg-sunken py-1 pr-1 pl-3">
                    <code translate="no" data-testid="totp-secret" className="min-w-0 flex-1 text-14 tracking-wide break-words text-fg select-all">
                      {groupSecret(enrollment.secret)}
                    </code>
                    <CopyButton value={enrollment.secret} label="Copy the key" />
                  </div>
                </div>
                <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-13">
                  <dt className="text-fg-muted">Account</dt>
                  <dd translate="no" className="mono text-12 text-fg">
                    WASM:{hostname}
                  </dd>
                  <dt className="text-fg-muted">Type</dt>
                  <dd className="text-fg">Time-based, 6 digits, every 30 seconds</dd>
                </dl>
              </div>
            </div>
          </li>
          <li className="flex flex-col gap-3">
            <p className="text-14 font-medium text-fg">2. Enter the code the app shows</p>
            <form id={formId} noValidate onSubmit={submit}>
              <Field label="Authentication code" error={codeError.fields.code}>
                <Input
                  ref={codeRef}
                  mono
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  spellCheck={false}
                  maxLength={8}
                  placeholder="123456"
                  value={code}
                  onValueChange={(value: string) => {
                    setCode(value);
                    if (confirm.isError) confirm.reset();
                  }}
                  className="w-40"
                />
              </Field>
            </form>
            {codeError.form !== null ? (
              <ErrorBlock live compact error={codeError.form} title="Could not turn on two-factor authentication" />
            ) : null}
          </li>
        </ol>
      ) : null}
    </Dialog>
  );
}

// ---------------------------------------------------------------------------------------
// Backup codes, shown once

interface BackupCodesDialogProps {
  open: boolean;
  codes: readonly string[];
  hostname: string;
  description: string;
  saved: boolean;
  /** The operator tried to close without ticking the box: say why they cannot yet. */
  nudge: boolean;
  savedRef: RefObject<HTMLDivElement | null>;
  onSavedChange: (saved: boolean) => void;
  onOpenChange: (open: boolean) => void;
}

/**
 * A set of backup codes, the only time it is shown: copy, download, and a box to tick before
 * the dialog lets go, since closing it loses the codes for good. Used when two-factor is turned
 * on and when a new set replaces the old one.
 */
function BackupCodesDialog({ open, codes, hostname, description, saved, nudge, savedRef, onSavedChange, onOpenChange }: BackupCodesDialogProps) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="md"
      title="Save your backup codes"
      description={description}
      footer={
        <Button
          variant="primary"
          disabled={!saved}
          onClick={() => {
            onOpenChange(false);
          }}
        >
          Done
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <ul aria-label="Backup codes" className="grid grid-cols-2 gap-x-6 gap-y-2 rounded-control border border-border bg-bg-sunken px-4 py-3">
          {codes.map((backup) => (
            <li key={backup} translate="no" className="mono text-14 tracking-wide text-fg select-all">
              {backup}
            </li>
          ))}
        </ul>
        <div className="flex flex-wrap items-center gap-2">
          <CopyTextButton value={codes.join("\n")} size="sm">
            Copy codes
          </CopyTextButton>
          <Button
            size="sm"
            icon={<Download aria-hidden="true" />}
            onClick={() => {
              downloadText(`wasm-backup-codes-${hostname}.txt`, backupCodesFile(codes, hostname));
            }}
          >
            Download as text
          </Button>
        </div>
        <div ref={savedRef} className={cx("rounded-control border p-3", nudge ? "border-warn/50 bg-warn-soft" : "border-transparent")}>
          <Checkbox label="I have saved these codes somewhere safe" checked={saved} onCheckedChange={onSavedChange} />
          {nudge ? (
            <p role="alert" className="mt-2 flex items-start gap-2 text-13 text-fg">
              <TriangleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-warn" />
              Save the codes and tick the box first. They cannot be shown again.
            </p>
          ) : null}
        </div>
      </div>
    </Dialog>
  );
}

/**
 * A new set of backup codes: asked for first (the old codes stop working), then the server
 * asks for "Confirm it's you" if the session is not elevated, then the new set, shown once.
 */
function RegenerateCodesDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const refresh = useRefreshTwoFactor();
  const { data: session } = useQuery(sessionQuery());
  const hostname = session?.hostname ?? "this server";
  const [codes, setCodes] = useState<readonly string[] | null>(null);
  const [saved, setSaved] = useState(false);
  const [nudge, setNudge] = useState(false);
  const savedRef = useRef<HTMLDivElement>(null);

  const regenerate = useMutation({
    mutationFn: regenerateBackupCodes,
    onSuccess: (result) => {
      setCodes(result.backup_codes);
      refresh();
    },
  });

  const onOpenChange = (next: boolean): void => {
    if (next || regenerate.isPending) return;
    if (codes !== null && !saved) {
      setNudge(true);
      savedRef.current?.querySelector<HTMLElement>("[role=checkbox]")?.focus();
      return;
    }
    if (codes !== null) toast.success("Replaced the backup codes");
    onClose();
  };

  if (codes !== null) {
    return (
      <BackupCodesDialog
        open={open}
        codes={codes}
        hostname={hostname}
        description="The old codes no longer work. Each of these signs in once if your authenticator is lost; this is the only time they are shown."
        saved={saved}
        nudge={nudge}
        savedRef={savedRef}
        onSavedChange={(next) => {
          setSaved(next);
          if (next) setNudge(false);
        }}
        onOpenChange={onOpenChange}
      />
    );
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="sm"
      title="Replace your backup codes?"
      description="A new set of backup codes is made and the old ones stop working, used or not. Keep the new set somewhere safe."
      footer={
        <>
          <Button
            disabled={regenerate.isPending}
            onClick={() => {
              onOpenChange(false);
            }}
          >
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={regenerate.isPending}
            onClick={() => {
              regenerate.mutate();
            }}
          >
            Replace backup codes
          </Button>
        </>
      }
    >
      {regenerate.isError ? <ErrorBlock live compact error={regenerate.error} title="The backup codes were not replaced" /> : null}
    </Dialog>
  );
}

// ---------------------------------------------------------------------------------------
// Turning it off

function DisableDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const refresh = useRefreshTwoFactor();
  const [code, setCode] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const formId = useId();
  const disable = useMutation({
    mutationFn: (value: string) => disableTwoFactor(value),
    onSuccess: () => {
      refresh();
      toast.success("Turned off two-factor authentication");
      setCode("");
      onClose();
    },
    onError: () => {
      inputRef.current?.select();
    },
  });
  const errors = splitErrors(disable.error, ["code"], "code");

  const onOpenChange = (next: boolean): void => {
    // Pending includes "Confirm it's you", which opens over this dialog; a press inside it is
    // outside this one and must not close it.
    if (next || disable.isPending) return;
    setCode("");
    disable.reset();
    onClose();
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="sm"
      initialFocus={inputRef}
      title="Turn off two-factor authentication"
      description="Sign-in will ask only for the access token. Enter a code from your authenticator app, or one of your backup codes, to confirm."
      footer={
        <>
          <Button
            disabled={disable.isPending}
            onClick={() => {
              onOpenChange(false);
            }}
          >
            Cancel
          </Button>
          <Button type="submit" form={formId} variant="danger" loading={disable.isPending} disabled={code.trim() === ""}>
            Turn off
          </Button>
        </>
      }
    >
      <form
        id={formId}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          const value = code.trim();
          if (value !== "" && !disable.isPending) disable.mutate(value);
        }}
        className="flex flex-col gap-4"
      >
        <Field label="Authentication or backup code" error={errors.fields.code}>
          <Input
            ref={inputRef}
            mono
            autoComplete="one-time-code"
            spellCheck={false}
            value={code}
            onValueChange={(value: string) => {
              setCode(value);
              if (disable.isError) disable.reset();
            }}
            className="w-48"
          />
        </Field>
        {errors.form !== null ? <ErrorBlock live compact error={errors.form} title="Two-factor authentication is still on" /> : null}
      </form>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------------------

function Status({ status }: { status: TwoFactorStatus }) {
  if (status.enabled) {
    const few = status.backup_codes_remaining <= FEW_CODES;
    return (
      <div className="flex min-w-0 items-start gap-3">
        <ShieldCheck aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-ok" />
        <div className="flex min-w-0 flex-col gap-0.5">
          <p className="text-14 font-medium text-fg">On</p>
          <p className="text-13 text-fg-muted">Sign-in asks for a code from your authenticator app after the access token.</p>
          <p className={cx("mt-1 flex items-center gap-1.5 text-13", few ? "text-warn" : "text-fg-muted")}>
            {few ? <TriangleAlert aria-hidden="true" className="size-3.5" /> : null}
            <span className="tabular-nums">
              {`${String(status.backup_codes_remaining)} of ${String(BACKUP_CODES)} backup codes left`}
            </span>
          </p>
          {few ? <p className="text-13 text-fg-muted">Replace them with a new set before they run out.</p> : null}
        </div>
      </div>
    );
  }
  return (
    <div className="flex min-w-0 items-start gap-3">
      <ShieldOff aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-idle" />
      <div className="flex min-w-0 flex-col gap-0.5">
        <p className="text-14 font-medium text-fg">Off</p>
        <p className="text-13 text-fg-muted">Anyone holding the access token can sign in and act as root on this server.</p>
        {status.pending ? (
          <p className="mt-1 text-13 text-fg-muted">A setup was started and not finished. Setting up again makes a new key.</p>
        ) : null}
      </div>
    </div>
  );
}

/** Two-factor authentication: its state, and turning it on or off. */
export function TwoFactorSection() {
  const query = useQuery(twoFactorQuery());
  const [enrollment, setEnrollment] = useState<TwoFactorEnrollment | null>(null);
  const [enrolling, setEnrolling] = useState(false);
  const [disabling, setDisabling] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const enroll = useMutation({
    mutationFn: enrollTwoFactor,
    onSuccess: (result) => {
      setEnrollment(result);
      setEnrolling(true);
    },
    onError: (error) => {
      reportActionError("Could not start the setup", error);
    },
  });

  const status = query.data;
  let body;
  if (status !== undefined) {
    body = (
      <div className="flex flex-col gap-4 rounded-card border border-border bg-surface p-5 shadow-raised sm:flex-row sm:items-start sm:justify-between">
        <Status status={status} />
        <div className="flex shrink-0 flex-wrap gap-2">
          {status.enabled ? (
            <>
              <Button
                onClick={() => {
                  setRegenerating(true);
                }}
              >
                New backup codes
              </Button>
              <Button
                onClick={() => {
                  setDisabling(true);
                }}
              >
                Turn off
              </Button>
            </>
          ) : (
            <Button
              variant="primary"
              loading={enroll.isPending}
              onClick={() => {
                enroll.mutate();
              }}
            >
              Set up two-factor authentication
            </Button>
          )}
        </div>
      </div>
    );
  } else if (query.isError) {
    body = (
      <ErrorBlock
        error={query.error}
        title="Could not load the two-factor state"
        onRetry={() => void query.refetch()}
        retrying={query.isRefetching}
      />
    );
  } else {
    body = (
      <div aria-busy="true" className="flex gap-3 rounded-card border border-border bg-surface p-5 shadow-raised">
        <span className="sr-only">Loading the two-factor state</span>
        <Skeleton className="size-5" />
        <div className="flex flex-1 flex-col gap-2">
          <Skeleton className="h-4 w-16" />
          <Skeleton className="h-3 w-72 max-w-full" />
        </div>
      </div>
    );
  }

  return (
    <SettingsSection
      title="Two-factor authentication"
      description="A code from an authenticator app at every sign-in and whenever an action needs you to confirm it's you."
    >
      {body}
      <EnrollDialog
        key={enrollment?.secret ?? "none"}
        open={enrolling}
        enrollment={enrollment}
        onClose={() => {
          setEnrolling(false);
          // Closing loses the only handle on the secret and QR URI held here; forgetting it
          // also means the next "Set up" starts this dialog from a clean key.
          setEnrollment(null);
        }}
      />
      <DisableDialog
        open={disabling}
        onClose={() => {
          setDisabling(false);
        }}
      />
      {/* Mounted per opening: every replacement starts from a clean state. */}
      {regenerating ? (
        <RegenerateCodesDialog
          open
          onClose={() => {
            setRegenerating(false);
          }}
        />
      ) : null}
    </SettingsSection>
  );
}
