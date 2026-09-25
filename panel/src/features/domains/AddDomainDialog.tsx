import { useMutation, useQuery } from "@tanstack/react-query";
import { RotateCw } from "lucide-react";
import { useId, useState } from "react";
import type { SyntheticEvent } from "react";

import { isApiError, request } from "../../api/client";
import { dnsCheckQuery } from "../../api/queries/domains";
import type { AppDomainChange } from "../../api/queries/domains";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { Skeleton } from "../../components/ui/Skeleton";
import { cx } from "../../lib/cx";
import { DnsVerdict } from "./DnsVerdict";
import { dnsVerdict } from "./dns";
import { domainProblem, normalizeDomain } from "./names";

type AddableKind = "alias" | "redirect";

const KINDS: readonly { value: AddableKind; label: string; description: (app: string) => string }[] = [
  { value: "alias", label: "Alias", description: (app) => `Serves ${app} on this name too.` },
  {
    value: "redirect",
    label: "Redirect",
    description: (app) => `Sends visitors to ${app} with a permanent redirect (301).`,
  },
];

function KindChoice({ app, value, onChange }: { app: string; value: AddableKind; onChange: (kind: AddableKind) => void }) {
  const name = useId();
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1.5 text-13 font-medium text-fg">Role</legend>
      <div className="grid gap-2 sm:grid-cols-2">
        {KINDS.map((kind) => (
          <label
            key={kind.value}
            className={cx(
              "grid cursor-pointer grid-cols-[auto_minmax(0,1fr)] content-start items-start gap-x-2.5 gap-y-0.5 rounded-control border px-3 py-2.5",
              "has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-1 has-[:focus-visible]:outline-focus",
              value === kind.value ? "border-accent bg-accent-soft" : "border-border hover:bg-surface-hover",
            )}
          >
            <input
              type="radio"
              name={name}
              value={kind.value}
              checked={value === kind.value}
              onChange={() => onChange(kind.value)}
              className="row-span-2 mt-0.5 size-4 shrink-0 accent-accent"
            />
            <span className="text-13 font-medium text-fg">{kind.label}</span>
            <span className="col-start-2 text-12 text-pretty text-fg-muted">{kind.description(app)}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function VerdictSkeleton() {
  return (
    <div aria-busy="true" className="flex flex-col gap-3 rounded-card border border-border p-3">
      <span className="sr-only">Checking DNS</span>
      <Skeleton className="h-4 w-56" />
      <div className="grid gap-3 sm:grid-cols-2">
        <Skeleton className="h-10" />
        <Skeleton className="h-10" />
      </div>
    </div>
  );
}

export interface AddDomainDialogProps {
  /** The application's primary domain. */
  app: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The domain was added: the application's domains afterwards, and the name added. */
  onAdded: (change: AppDomainChange, name: string) => void;
}

/**
 * Adds a name to an application in two moves with one button: first it asks DNS where the
 * name points and shows the verdict, then it adds the name. A name that does not point here
 * can still be added; the dialog says what that costs (the certificate order fails until it
 * does) before the operator decides.
 */
export function AddDomainDialog({ app, open, onOpenChange, onAdded }: AddDomainDialogProps) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<AddableKind>("alias");
  const [checked, setChecked] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const dns = useQuery({ ...dnsCheckQuery(app, checked ?? ""), enabled: open && checked !== null });
  const add = useMutation({
    mutationFn: (domain: string) =>
      request("post", "/api/apps/{domain}/domains", { params: { domain: app }, body: { domain, kind } }),
    onSuccess: (change, domain) => {
      onAdded(change, domain);
      close(false, true);
    },
  });

  const normalized = normalizeDomain(name);
  const isChecked = checked !== null && checked === normalized;
  const verdict = isChecked && dns.data ? dnsVerdict(dns.data) : null;

  // `settled` closes it from the mutation's own success callback, which runs while the
  // mutation still reads as pending.
  function close(next: boolean, settled = false): void {
    if (!next && add.isPending && !settled) return;
    onOpenChange(next);
    if (!next) {
      setName("");
      setKind("alias");
      setChecked(null);
      setProblem(null);
      add.reset();
    }
  }

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    const invalid = domainProblem(name);
    if (invalid !== null) {
      setProblem(invalid);
      return;
    }
    if (normalized === app) {
      setProblem(`${app} is this application's primary domain already.`);
      return;
    }
    setProblem(null);
    if (!isChecked) {
      setChecked(normalized);
      return;
    }
    if (dns.isPending) return;
    add.mutate(normalized);
  };

  const fieldError = problem ?? (add.error && isApiError(add.error) ? (add.error.fields?.["domain"] ?? null) : null);
  const primaryLabel = !isChecked ? "Check DNS" : verdict === null || verdict === "here" ? "Add domain" : "Add anyway";
  const formId = useId();

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={`Add a domain to ${app}`}
      description="The site is rewritten and reloaded at once. When the app serves HTTPS, its certificate is extended to the new name."
      footer={
        <>
          <Button disabled={add.isPending} onClick={() => close(false)}>
            Cancel
          </Button>
          <Button
            type="submit"
            form={formId}
            variant="primary"
            loading={add.isPending || (isChecked && dns.isFetching && dns.data === undefined)}
          >
            {primaryLabel}
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} noValidate className="flex flex-col gap-5">
        <Field label="Domain" error={fieldError} description="The bare name, such as shop.example.com.">
          <Input
            mono
            value={name}
            onValueChange={(value: string) => {
              setName(value);
              setProblem(null);
            }}
            placeholder={`shop.${app}`}
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            inputMode="url"
          />
        </Field>
        <KindChoice app={app} value={kind} onChange={setKind} />

        {isChecked ? (
          <div className="flex flex-col gap-2" aria-live="polite">
            {dns.data ? (
              <DnsVerdict check={dns.data} />
            ) : dns.isError ? (
              <ErrorBlock compact error={dns.error} title={`Could not resolve ${normalized}`} />
            ) : (
              <VerdictSkeleton />
            )}
            {dns.data || dns.isError ? (
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-12 text-pretty text-fg-muted">
                  {verdict === "here"
                    ? "Adding it extends the certificate to it right away."
                    : "Added now, it is served at once, but its certificate fails until DNS points here. Adding it again retries."}
                </p>
                <Button
                  size="sm"
                  variant="ghost"
                  icon={<RotateCw aria-hidden="true" />}
                  loading={dns.isFetching}
                  onClick={() => void dns.refetch()}
                >
                  Check again
                </Button>
              </div>
            ) : null}
          </div>
        ) : null}

        {add.isError && fieldError === null ? (
          <ErrorBlock live compact error={add.error} title={`${normalized} was not added`} />
        ) : null}
      </form>
    </Dialog>
  );
}
