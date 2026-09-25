import { useMutation, useQueryClient } from "@tanstack/react-query";
import { TriangleAlert } from "lucide-react";
import { useId, useRef, useState } from "react";
import type { SyntheticEvent } from "react";

import { authKeys, createApiToken } from "../../api/queries/auth";
import type { CreatedToken } from "../../api/queries/auth";
import { CommandHint } from "../../components/page/CommandHint";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { toast } from "../../components/ui/toast";
import { cx } from "../../lib/cx";
import { CopyTextButton } from "./CopyTextButton";
import { splitErrors } from "./formErrors";
import { DEFAULT_EXPIRY, EXPIRY_OPTIONS, SCOPES, expiryPhrase } from "./tokens";
import type { TokenScope } from "./tokens";

export interface CreateTokenDialogProps {
  open: boolean;
  onClose: () => void;
}

const FIELDS = ["name", "scope", "expires_hours"] as const;

/** The scope choice: one card per scope, each saying what a token of that scope can do. */
function ScopePicker({ value, onChange, error }: { value: TokenScope; onChange: (scope: TokenScope) => void; error?: string | undefined }) {
  const name = useId();
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1.5 text-13 font-medium text-fg">Scope</legend>
      {SCOPES.map((scope) => (
        <label
          key={scope.value}
          className={cx(
            "grid cursor-pointer grid-cols-[auto_minmax(0,1fr)] items-start gap-x-3 gap-y-0.5 rounded-control border px-3 py-2.5",
            "has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-1 has-[:focus-visible]:outline-focus",
            value === scope.value ? "border-accent bg-accent-soft" : "border-border hover:bg-surface-hover",
          )}
        >
          <input
            type="radio"
            name={name}
            value={scope.value}
            checked={value === scope.value}
            onChange={() => {
              onChange(scope.value);
            }}
            aria-labelledby={`${name}-${scope.value}-label`}
            aria-describedby={`${name}-${scope.value}`}
            className="row-span-2 mt-0.5 size-4 shrink-0 accent-accent"
          />
          <span id={`${name}-${scope.value}-label`} className="text-14 font-medium text-fg">
            {scope.label}
          </span>
          <span id={`${name}-${scope.value}`} className="col-start-2 text-13 text-fg-muted">
            {scope.description}
          </span>
        </label>
      ))}
      {error !== undefined ? <p className="text-13 text-fail">{error}</p> : null}
    </fieldset>
  );
}

/** The token, once: copy it now, because only its hash is kept. */
function TokenOnce({ token }: { token: CreatedToken }) {
  const labelId = useId();
  const example = `curl -H "Authorization: Bearer ${token.token}" ${window.location.origin}/api/apps`;
  return (
    <div className="flex flex-col gap-4">
      <div role="alert" className="flex items-start gap-2.5 rounded-control border border-warn/40 bg-warn-soft px-3 py-2.5">
        <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warn" />
        <p className="text-13 text-fg">
          This is the only time the token is shown. Put it in your CI's secret store now: WASM keeps only a hash of it, and
          a lost token can only be revoked and replaced.
        </p>
      </div>
      <div className="flex flex-col gap-1.5">
        <span id={labelId} className="text-13 font-medium text-fg">{`Token for ${token.name}`}</span>
        {/* All of it, wrapped: a token cut off by a narrow field cannot be checked by eye. */}
        <code
          aria-labelledby={labelId}
          translate="no"
          data-testid="new-token"
          className="rounded-control border border-border-strong bg-surface px-3 py-2 text-13 break-all text-fg select-all"
        >
          {token.token}
        </code>
      </div>
      <div>
        <CopyTextButton value={token.token} variant="primary">
          Copy token
        </CopyTextButton>
      </div>
      <CommandHint label="Try it" command={example} />
    </div>
  );
}

/** Issuing an API token: a name, a scope and an expiry, then the token itself, once. */
export function CreateTokenDialog({ open, onClose }: CreateTokenDialogProps) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [scope, setScope] = useState<TokenScope>("read");
  const [expiry, setExpiry] = useState(DEFAULT_EXPIRY);
  const [created, setCreated] = useState<CreatedToken | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const formId = useId();

  const create = useMutation({
    mutationFn: () =>
      createApiToken({
        name: name.trim(),
        scope,
        expires_hours: EXPIRY_OPTIONS.find((option) => option.value === expiry)?.hours ?? null,
      }),
    onSuccess: (token) => {
      setCreated(token);
      void queryClient.invalidateQueries({ queryKey: authKeys.tokens });
    },
  });
  const errors = splitErrors(create.error, FIELDS);

  const reset = (): void => {
    setName("");
    setScope("read");
    setExpiry(DEFAULT_EXPIRY);
    setCreated(null);
    create.reset();
  };

  const onOpenChange = (next: boolean): void => {
    // Pending includes "Confirm it's you", which opens over this dialog; a press inside it is
    // outside this one and must not close it.
    if (next || create.isPending) return;
    if (created !== null) toast.success(`Created token ${created.name}`);
    onClose();
    reset();
  };

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (create.isPending) return;
    create.mutate();
  };

  if (created !== null) {
    return (
      <Dialog
        open={open}
        onOpenChange={onOpenChange}
        size="md"
        title="Copy your new token"
        description={`${created.name}, ${created.scope} scope, ${expiryPhrase(created.expires_at ?? null)}.`}
        footer={
          <Button
            onClick={() => {
              onOpenChange(false);
            }}
          >
            Done
          </Button>
        }
      >
        <TokenOnce token={created} />
      </Dialog>
    );
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="md"
      initialFocus={nameRef}
      title="Create an API token"
      description="For CI and scripts: sent as a Bearer token, it acts with its scope and nothing more."
      footer={
        <>
          <Button
            disabled={create.isPending}
            onClick={() => {
              onOpenChange(false);
            }}
          >
            Cancel
          </Button>
          <Button type="submit" form={formId} variant="primary" loading={create.isPending} disabled={name.trim() === ""}>
            Create token
          </Button>
        </>
      }
    >
      <form id={formId} noValidate onSubmit={submit} className="flex flex-col gap-5">
        {errors.form !== null ? <ErrorBlock live compact error={errors.form} title="Could not create the token" /> : null}
        <Field label="Name" description="After what will hold it, such as ci-deploy. Names are never reused." error={errors.fields.name}>
          <Input
            ref={nameRef}
            mono
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            maxLength={64}
            value={name}
            onValueChange={(value: string) => {
              setName(value);
            }}
          />
        </Field>
        <ScopePicker value={scope} onChange={setScope} error={errors.fields.scope} />
        <Field label="Expires" nativeLabel={false} error={errors.fields.expires_hours}>
          <Select
            options={EXPIRY_OPTIONS}
            value={expiry}
            onValueChange={(value) => {
              setExpiry(value);
            }}
            className="w-44"
          />
        </Field>
      </form>
    </Dialog>
  );
}
