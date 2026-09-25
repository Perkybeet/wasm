import { useMutation } from "@tanstack/react-query";
import { useId, useState } from "react";
import type { SyntheticEvent } from "react";

import { isApiError, request } from "../../api/client";
import { ErrorBlock } from "../../components/page/QueryState";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { Checkbox } from "../../components/ui/Checkbox";
import { Dialog } from "../../components/ui/Dialog";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { Textarea } from "../../components/ui/Textarea";
import { domainProblem, normalizeDomain, parseNames, wwwOf } from "./names";

export type CertMethod = "auto" | "nginx" | "apache" | "webroot" | "standalone";

export const CERT_METHODS: readonly { value: CertMethod; label: string; hint: string }[] = [
  { value: "auto", label: "Automatic", hint: "The plugin of the web server that is running" },
  { value: "nginx", label: "nginx plugin", hint: "certbot --nginx" },
  { value: "apache", label: "Apache plugin", hint: "certbot --apache" },
  { value: "webroot", label: "Webroot", hint: "Challenge files in a directory the site serves" },
  { value: "standalone", label: "Standalone", hint: "certbot answers on port 80 itself" },
];

export interface IssueRequest {
  domain: string;
  email: string | null;
  domains: string[];
  method: string | null;
  webroot: string | null;
  include_www: boolean;
  expand: boolean;
}

export interface IssueForm {
  domain: string;
  names: string;
  includeWww: boolean;
  method: CertMethod;
  webroot: string;
  email: string;
  expand: boolean;
}

export type IssueErrors = Partial<Record<"domain" | "names" | "webroot" | "email", string>>;

/**
 * The form as the API takes it, or what is wrong with it. Pure, so the translation from what
 * the operator typed to the request is tested on its own.
 */
export function issueRequest(form: IssueForm): { request: IssueRequest } | { errors: IssueErrors } {
  const errors: IssueErrors = {};
  const domain = normalizeDomain(form.domain);
  const primaryProblem = domainProblem(domain);
  if (primaryProblem !== null) errors.domain = primaryProblem;
  const names = parseNames(form.names).filter((name) => name !== domain);
  const bad = names.find((name) => domainProblem(name) !== null);
  if (bad !== undefined) errors.names = `${bad}: ${domainProblem(bad) ?? ""}`;
  if (form.method === "webroot" && form.webroot.trim() === "") errors.webroot = "Enter the directory the site serves, such as /var/www/html.";
  else if (form.method === "webroot" && !form.webroot.trim().startsWith("/")) errors.webroot = "Enter an absolute path, starting with /.";
  const email = form.email.trim();
  if (email !== "" && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) errors.email = "Enter an address such as ops@example.com, or leave it empty.";
  if (Object.keys(errors).length > 0) return { errors };
  const www = wwwOf(domain);
  return {
    request: {
      domain,
      email: email === "" ? null : email,
      domains: names,
      method: form.method === "auto" ? null : form.method,
      webroot: form.method === "webroot" ? form.webroot.trim() : null,
      include_www: form.includeWww && www !== null && !names.includes(www),
      expand: form.expand,
    },
  };
}

const EMPTY: IssueForm = { domain: "", names: "", includeWww: true, method: "auto", webroot: "", email: "", expand: false };

export interface IssueCertificateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Issuance was queued: the job's id and the certificate's name. */
  onQueued: (jobId: string, domain: string) => void;
}

/** Orders a certificate from Let's Encrypt for a name and any others, as a job. */
export function IssueCertificateDialog({ open, onOpenChange, onQueued }: IssueCertificateDialogProps) {
  const [form, setForm] = useState<IssueForm>(EMPTY);
  const [errors, setErrors] = useState<IssueErrors>({});
  const formId = useId();

  const issue = useMutation({
    mutationFn: ({ domain, ...body }: IssueRequest) => request("post", "/api/certs/{domain}", { params: { domain }, body }),
    onSuccess: (result, sent) => {
      onQueued(result.job_id, sent.domain);
      close(false, true);
    },
  });

  // `settled` closes it from the mutation's own success callback, which runs while the
  // mutation still reads as pending.
  function close(next: boolean, settled = false): void {
    if (!next && issue.isPending && !settled) return;
    onOpenChange(next);
    if (!next) {
      setForm(EMPTY);
      setErrors({});
      issue.reset();
    }
  }

  const set = (patch: Partial<IssueForm>): void => {
    setForm((current) => ({ ...current, ...patch }));
    // Editing a field clears its own complaint, and only its own.
    setErrors((current) =>
      Object.fromEntries(Object.entries(current).filter(([key]) => !(key in patch))),
    );
  };

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    const result = issueRequest(form);
    if ("errors" in result) {
      setErrors(result.errors);
      return;
    }
    issue.mutate(result.request);
  };

  const serverFields = issue.error && isApiError(issue.error) ? (issue.error.fields ?? {}) : {};
  const fieldError = (name: keyof IssueErrors, server: string = name): string | undefined => errors[name] ?? serverFields[server];
  const www = wwwOf(form.domain);
  const extra = parseNames(form.names).filter((name) => name !== normalizeDomain(form.domain));

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      size="lg"
      title="Issue a certificate"
      description="Let's Encrypt checks that every name points to this server before it signs. Issuing runs as a job; the certificate appears here when it ends."
      footer={
        <>
          <Button disabled={issue.isPending} onClick={() => close(false)}>
            Cancel
          </Button>
          <Button type="submit" form={formId} variant="primary" loading={issue.isPending}>
            Issue certificate
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} noValidate className="flex flex-col gap-5">
        <div className="flex flex-col gap-2">
          <Field label="Domain" error={fieldError("domain")}>
            <Input
              mono
              value={form.domain}
              onValueChange={(value: string) => set({ domain: value })}
              placeholder="example.com"
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
            />
          </Field>
          {www !== null ? (
            <Checkbox
              label={`Also cover ${normalizeDomain(form.domain) === "" ? "the www name" : www}`}
              checked={form.includeWww}
              onCheckedChange={(checked) => set({ includeWww: checked })}
            />
          ) : null}
        </div>
        <Field
          label="Other names"
          optional
          error={fieldError("names", "domains")}
          description="Separated by spaces, commas or new lines. Each must point to this server too."
        >
          <Textarea
            mono
            rows={2}
            value={form.names}
            onChange={(event) => set({ names: event.target.value })}
            placeholder="shop.example.com, api.example.com"
            autoCapitalize="off"
          />
        </Field>
        {extra.length > 0 ? (
          <ul aria-label="Other names on the certificate" className="-mt-3 flex flex-wrap gap-1.5">
            {extra.map((name) => (
              <li key={name}>
                <Badge mono tone={domainProblem(name) === null ? "neutral" : "fail"}>
                  {name}
                </Badge>
              </li>
            ))}
          </ul>
        ) : null}
        <div className="grid gap-5 sm:grid-cols-2">
          <Field
            label="How to prove control"
            nativeLabel={false}
            description={CERT_METHODS.find((method) => method.value === form.method)?.hint}
          >
            <Select<CertMethod>
              options={CERT_METHODS}
              value={form.method}
              onValueChange={(method) => set({ method })}
              className="w-full"
            />
          </Field>
          <Field label="Email" optional error={fieldError("email")} description="For expiry warnings. Empty uses Settings.">
            <Input
              type="email"
              value={form.email}
              onValueChange={(value: string) => set({ email: value })}
              placeholder="ops@example.com"
              autoComplete="email"
            />
          </Field>
        </div>
        {form.method === "webroot" ? (
          <Field label="Webroot" error={fieldError("webroot")} description="Certbot writes its challenge files here.">
            <Input
              mono
              value={form.webroot}
              onValueChange={(value: string) => set({ webroot: value })}
              placeholder="/var/www/html"
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
        ) : null}
        <Checkbox
          label="Order again even if a certificate already covers these names"
          description="Certbot's --expand: reissued under the same name."
          checked={form.expand}
          onCheckedChange={(checked) => set({ expand: checked })}
        />
        {issue.isError && Object.keys(serverFields).length === 0 ? (
          <ErrorBlock live compact error={issue.error} title="Issuing was not queued" />
        ) : null}
      </form>
    </Dialog>
  );
}
