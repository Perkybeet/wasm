import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { UseQueryResult } from "@tanstack/react-query";
import { CircleAlert, FileCog } from "lucide-react";
import type { ReactNode } from "react";

import {
  appsDirectoryQuery,
  backupSettingsQuery,
  configKeys,
  configQuery,
  saveAppsDirectory,
  saveBackupSettings,
  saveSslSettings,
  saveWebSettings,
  saveWebserver,
  sslSettingsQuery,
  webSettingsQuery,
  webserverQuery,
} from "../../api/queries/config";
import type { ConfigSection } from "../../api/queries/config";
import { useDocumentTitle } from "../../app/documentTitle";
import { ErrorBlock } from "../../components/page/QueryState";
import { Sections } from "../../components/page/Section";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import type { SelectOption } from "../../components/ui/Select";
import { Skeleton } from "../../components/ui/Skeleton";
import { toast } from "../../components/ui/toast";
import { configGetCommand, configSetCommand } from "./shell";
import { SettingsFormCard, SettingsSection } from "./SettingsForm";
import { useSettingsForm } from "./useSettingsForm";
import type { FormValues, SettingsForm } from "./useSettingsForm";

/**
 * A whole number typed into a field, as a number; anything else is sent as typed, and the
 * server's refusal is shown beside the field. The server is the one validator.
 */
function wholeNumber(value: string): number {
  const trimmed = value.trim();
  return /^-?\d+$/.test(trimmed) ? Number(trimmed) : (trimmed as unknown as number);
}

/** The terminal form of a section: what `wasm config set` would change, or how to read it. */
function commandsFor<V extends FormValues>(form: SettingsForm<V>, keys: Record<keyof V & string, string>, read: string): string[] {
  if (!form.dirty || form.values === undefined) return [configGetCommand(read)];
  const values = form.values;
  return form.changed.map((name) => configSetCommand(keys[name], values[name] ?? ""));
}

/** The loading shape of a section's form: labels and controls still to come. */
function FormSkeleton({ fields }: { fields: number }) {
  return (
    <div aria-hidden="true" className="flex flex-col gap-5 rounded-card border border-border bg-surface p-5 shadow-raised">
      {Array.from({ length: fields }, (_, index) => (
        <div key={index} className="flex flex-col gap-2">
          <Skeleton className="h-3 w-32" />
          <Skeleton className="h-8 w-full max-w-md" />
        </div>
      ))}
      <Skeleton className="ml-auto h-8 w-28" />
    </div>
  );
}

/** A section's form once its settings are loaded; the skeleton or the failure until then. */
function Loaded<T>({ query, label, fields, children }: { query: UseQueryResult<T>; label: string; fields: number; children: ReactNode }) {
  if (query.data !== undefined) return <>{children}</>;
  if (query.isError) {
    return (
      <ErrorBlock
        error={query.error}
        title={`Could not load ${label}`}
        onRetry={() => void query.refetch()}
        retrying={query.isRefetching}
      />
    );
  }
  return (
    <div aria-busy="true">
      <span className="sr-only">{`Loading ${label}`}</span>
      <FormSkeleton fields={fields} />
    </div>
  );
}

/** After a save: the cached answer becomes what was saved, then everything refreshes. */
function useSaved() {
  const queryClient = useQueryClient();
  return (section: ConfigSection, saved: unknown, message: string, description?: string): void => {
    queryClient.setQueryData(configKeys.section(section), saved);
    void queryClient.invalidateQueries({ queryKey: configKeys.all });
    toast.success(message, description !== undefined ? { description } : undefined);
  };
}

// ---------------------------------------------------------------------------------------

function AppsDirectorySection() {
  const query = useQuery(appsDirectoryQuery());
  const saved = useSaved();
  const form = useSettingsForm({
    server: query.data ? { apps_directory: query.data.apps_directory } : undefined,
    names: ["apps_directory"],
    soleField: "apps_directory",
    save: async ({ apps_directory }) => {
      const result = await saveAppsDirectory({ apps_directory: apps_directory.trim() });
      saved("apps-directory", { apps_directory: result.apps_directory }, "Saved the applications directory");
    },
  });
  return (
    <SettingsSection
      title="Applications directory"
      description="Where new applications are cloned, built and run from. Applications that already exist keep their own path."
      commands={commandsFor(form, { apps_directory: "apps_directory" }, "apps_directory")}
    >
      <Loaded query={query} label="the applications directory" fields={1}>
        <SettingsFormCard {...cardProps(form, "Could not save the applications directory")}>
          <Field label="Directory" description="An absolute path, such as /var/www/apps." error={form.fieldErrors.apps_directory}>
            <Input
              mono
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              value={form.values?.apps_directory ?? ""}
              onValueChange={(value: string) => {
                form.set("apps_directory", value);
              }}
              className="max-w-md"
            />
          </Field>
        </SettingsFormCard>
      </Loaded>
    </SettingsSection>
  );
}

const WEBSERVERS: readonly SelectOption[] = [
  { value: "nginx", label: "Nginx" },
  { value: "apache", label: "Apache" },
];

function WebserverSection() {
  const query = useQuery(webserverQuery());
  const saved = useSaved();
  const form = useSettingsForm({
    server: query.data ? { webserver: query.data.webserver } : undefined,
    names: ["webserver"],
    soleField: "webserver",
    save: async ({ webserver }) => {
      const result = await saveWebserver({ webserver });
      saved("webserver", { webserver: result.webserver }, "Saved the web server");
    },
  });
  return (
    <SettingsSection
      title="Web server"
      description="Serves the sites WASM creates and terminates their HTTPS. Sites that already exist stay on the server they were created for."
      commands={commandsFor(form, { webserver: "webserver" }, "webserver")}
    >
      <Loaded query={query} label="the web server" fields={1}>
        <SettingsFormCard {...cardProps(form, "Could not save the web server")}>
          <Field label="Web server for new sites" nativeLabel={false} error={form.fieldErrors.webserver}>
            <Select
              options={WEBSERVERS}
              value={form.values?.webserver ?? null}
              onValueChange={(value) => {
                form.set("webserver", value);
              }}
              className="w-56"
            />
          </Field>
        </SettingsFormCard>
      </Loaded>
    </SettingsSection>
  );
}

function CertificatesSection() {
  const query = useQuery(sslSettingsQuery());
  const saved = useSaved();
  const form = useSettingsForm({
    server: query.data ? { email: query.data.email } : undefined,
    names: ["email"],
    soleField: "email",
    save: async ({ email }) => {
      // The endpoint replaces the whole block; the two values this form does not show are
      // sent back exactly as the server gave them.
      const current = query.data ?? { enabled: true, provider: "certbot", email: "" };
      const body = { enabled: current.enabled, provider: current.provider, email: email.trim() };
      await saveSslSettings(body);
      saved("ssl", body, "Saved the certificate email");
    },
  });
  return (
    <SettingsSection
      title="Certificates"
      description={
        <>
          HTTPS certificates are requested from Let's Encrypt with <span className="mono text-12">{query.data?.provider ?? "certbot"}</span>
          . The account they are issued under is registered with this email.
        </>
      }
      commands={commandsFor(form, { email: "ssl.email" }, "ssl.email")}
    >
      <Loaded query={query} label="the certificate settings" fields={1}>
        <SettingsFormCard {...cardProps(form, "Could not save the certificate email")}>
          <Field
            label="Email for certificate notices"
            optional
            description="Let's Encrypt writes here about certificates about to expire and problems with the account. Empty registers without an email."
            error={form.fieldErrors.email}
          >
            <Input
              type="email"
              autoComplete="email"
              spellCheck={false}
              placeholder="ops@example.com"
              value={form.values?.email ?? ""}
              onValueChange={(value: string) => {
                form.set("email", value);
              }}
              className="max-w-md"
            />
          </Field>
        </SettingsFormCard>
      </Loaded>
    </SettingsSection>
  );
}

function BackupsSection() {
  const query = useQuery(backupSettingsQuery());
  const saved = useSaved();
  const form = useSettingsForm({
    server: query.data ? { directory: query.data.directory, max_per_app: String(query.data.max_per_app) } : undefined,
    names: ["directory", "max_per_app"],
    save: async ({ directory, max_per_app }) => {
      const body = { directory: directory.trim(), max_per_app: wholeNumber(max_per_app) };
      await saveBackupSettings(body);
      saved("backup", body, "Saved the backup settings");
    },
  });
  return (
    <SettingsSection
      title="Backups"
      description="Where backups of applications are written and how many are kept. After each backup, the oldest ones past the limit are deleted."
      commands={commandsFor(form, { directory: "backup.directory", max_per_app: "backup.max_per_app" }, "backup")}
    >
      <Loaded query={query} label="the backup settings" fields={2}>
        <SettingsFormCard {...cardProps(form, "Could not save the backup settings")}>
          <Field label="Backup directory" error={form.fieldErrors.directory}>
            <Input
              mono
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              value={form.values?.directory ?? ""}
              onValueChange={(value: string) => {
                form.set("directory", value);
              }}
              className="max-w-md"
            />
          </Field>
          <Field
            label="Backups kept per application"
            description="From 1 to 100."
            error={form.fieldErrors.max_per_app}
          >
            <Input
              type="number"
              inputMode="numeric"
              mono
              value={form.values?.max_per_app ?? ""}
              onValueChange={(value: string) => {
                form.set("max_per_app", value);
              }}
              className="w-32"
            />
          </Field>
        </SettingsFormCard>
      </Loaded>
    </SettingsSection>
  );
}

function ConsoleAddressSection() {
  const query = useQuery(webSettingsQuery());
  const saved = useSaved();
  const form = useSettingsForm({
    server: query.data ? { host: query.data.host, port: String(query.data.port) } : undefined,
    names: ["host", "port"],
    save: async ({ host, port }) => {
      // session_timeout rides along unchanged: the endpoint replaces the three together.
      const body = { host: host.trim(), port: wholeNumber(port), session_timeout: query.data?.session_timeout ?? 3600 };
      await saveWebSettings(body);
      saved("web", body, "Saved the console address");
    },
  });
  return (
    <SettingsSection
      title="Console address"
      description={
        <>
          The address commands such as <span className="mono text-12">wasm status --open</span> print when they link to
          this console. It does not move the console: <span className="mono text-12">wasm web start --host --port</span>{" "}
          decides where it listens.
        </>
      }
      commands={commandsFor(form, { host: "web.host", port: "web.port" }, "web.host")}
    >
      <Loaded query={query} label="the console address" fields={2}>
        <SettingsFormCard {...cardProps(form, "Could not save the console address")}>
          <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_8rem]">
            <Field label="Host" error={form.fieldErrors.host}>
              <Input
                mono
                autoComplete="off"
                autoCapitalize="off"
                spellCheck={false}
                value={form.values?.host ?? ""}
                onValueChange={(value: string) => {
                  form.set("host", value);
                }}
              />
            </Field>
            <Field label="Port" error={form.fieldErrors.port}>
              <Input
                type="number"
                inputMode="numeric"
                mono
                value={form.values?.port ?? ""}
                onValueChange={(value: string) => {
                  form.set("port", value);
                }}
              />
            </Field>
          </div>
        </SettingsFormCard>
      </Loaded>
    </SettingsSection>
  );
}

function cardProps<V extends FormValues>(form: SettingsForm<V>, errorTitle: string) {
  return {
    dirty: form.dirty,
    pending: form.pending,
    formError: form.formError,
    errorTitle,
    onSubmit: form.submit,
    onDiscard: form.discard,
  };
}

/** Where the settings live on disk, and whether the console can write them. */
function ConfigFileLine() {
  const { data } = useQuery(configQuery());
  if (data === undefined) return <Skeleton className="h-4 w-72" />;
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-13 text-fg-muted">
      <span className="flex min-w-0 items-start gap-2">
        <FileCog aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-fg-faint" />
        <span className="min-w-0">
          Saved to <span className="mono text-12 break-all text-fg">{data.path}</span>
        </span>
      </span>
      {data.writable ? null : (
        <span className="flex items-center gap-1.5 text-fail">
          <CircleAlert aria-hidden="true" className="size-3.5" />
          The console cannot write this file, so saving will fail. Check its owner and permissions.
        </span>
      )}
    </div>
  );
}

/** Settings > General: how WASM lays out, serves, secures and backs up applications. */
export function GeneralSettings() {
  useDocumentTitle("General settings", 1);
  return (
    <Sections>
      <ConfigFileLine />
      <AppsDirectorySection />
      <WebserverSection />
      <CertificatesSection />
      <BackupsSection />
      <ConsoleAddressSection />
    </Sections>
  );
}
