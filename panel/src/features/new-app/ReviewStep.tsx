import { TriangleAlert } from "lucide-react";
import { useId } from "react";
import type { ReactNode, Ref, SyntheticEvent } from "react";

import { Section } from "../../components/page/Section";
import { SegmentedControl } from "../../components/page/SegmentedControl";
import { Button } from "../../components/ui/Button";
import { Checkbox } from "../../components/ui/Checkbox";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { cx } from "../../lib/cx";
import { normalizeDomain } from "../domains/names";
import { EnvironmentFields } from "./EnvironmentFields";
import { InspectionReadout } from "./InspectionReadout";
import { hasPort, typeName, typeOptions } from "./wizard";
import type { Inspection, Layout, ReviewErrors, ReviewForm, WebServer } from "./wizard";

const LAYOUTS: readonly { value: Layout; label: string; description: string }[] = [
  {
    value: "releases",
    label: "Releases",
    description: "Each deploy builds beside the running copy and switches over only once it answers. Rolling back takes seconds.",
  },
  {
    value: "inplace",
    label: "In place",
    description: "Each deploy builds over the running copy, as WASM 1.x did. Rolling back restores a backup.",
  },
];

function Choice<V extends string>({
  legend,
  options,
  value,
  onChange,
  badge,
}: {
  legend: string;
  options: readonly { value: V; label: string; description: string }[];
  value: V;
  onChange: (value: V) => void;
  badge?: Partial<Record<V, string>>;
}) {
  const name = useId();
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1.5 text-13 font-medium text-fg">{legend}</legend>
      <div className="grid gap-2 sm:grid-cols-2">
        {options.map((option) => (
          <label
            key={option.value}
            className={cx(
              "grid cursor-pointer grid-cols-[auto_minmax(0,1fr)] content-start items-start gap-x-2.5 gap-y-0.5 rounded-control border px-3 py-2.5",
              "has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-1 has-[:focus-visible]:outline-focus",
              value === option.value ? "border-accent bg-accent-soft" : "border-border bg-surface hover:bg-surface-hover",
            )}
          >
            <input
              type="radio"
              name={name}
              value={option.value}
              checked={value === option.value}
              onChange={() => onChange(option.value)}
              className="row-span-2 mt-0.5 size-4 shrink-0 accent-accent"
            />
            <span className="flex items-center gap-2 text-13 font-medium text-fg">
              {option.label}
              {badge?.[option.value] !== undefined ? <span className="text-12 font-normal text-fg-muted">{badge[option.value]}</span> : null}
            </span>
            <span className="col-start-2 text-12 text-pretty text-fg-muted">{option.description}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

/** Where the proposed port came from, so a number that is not the framework's default is explained. */
function portNote(inspection: Inspection, taken: ReadonlyMap<number, string>): string {
  const preferred = inspection.default_port;
  const owner = taken.get(preferred);
  const base = "The app listens here and the web server passes requests to it.";
  if (inspection.detected_types.length === 0) return base;
  if (owner === undefined) return `${base} ${typeName(inspection.app_type)} uses ${String(preferred)} by default.`;
  return `${base} ${typeName(inspection.app_type)} uses ${String(preferred)} by default, which ${owner} has, so the next free one is proposed.`;
}

function Group({ title, description, children }: { title: string; description?: ReactNode; children: ReactNode }) {
  return (
    <Section title={title} level={3} {...(description !== undefined ? { description } : {})} className="border-t border-border pt-6">
      {children}
    </Section>
  );
}

export interface ReviewStepProps {
  inspection: Inspection;
  /** Ports other apps on this machine hold, and which. */
  taken: ReadonlyMap<number, string>;
  source: string;
  form: ReviewForm;
  errors: ReviewErrors;
  onChange: (form: ReviewForm) => void;
  onBack: () => void;
  onContinue: () => void;
  headingRef: Ref<HTMLHeadingElement>;
}

/**
 * Step two: what the inspection proposes, every part of it editable. The detected type is a
 * choice, never a silent guess; the commands are shown as they will run; the environment is a
 * form generated from `.env.example`. The source is not asked again.
 */
export function ReviewStep({ inspection, taken, source, form, errors, onChange, onBack, onContinue, headingRef }: ReviewStepProps) {
  const set = (patch: Partial<ReviewForm>): void => {
    onChange({ ...form, ...patch });
  };
  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    onContinue();
  };
  const detected = inspection.detected_types.length > 0;
  const chosenElsewhere = detected && form.appType !== inspection.app_type;
  const domain = normalizeDomain(form.domain);

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h2 ref={headingRef} tabIndex={-1} className="title text-18 text-fg outline-none">
          Review
        </h2>
        <p className="text-14 text-pretty text-fg-muted">Check what WASM found and fill in what only you know. Everything here can be changed later.</p>
      </header>

      <InspectionReadout inspection={inspection} source={source} />

      <Field
        label="Deploy as"
        nativeLabel={false}
        error={errors["appType"]}
        description="The type decides how the app is installed, built and started. Check it: detection reads files, not intent."
        className="sm:max-w-96"
      >
        <Select
          options={typeOptions(inspection.detected_types)}
          value={form.appType === "" ? null : form.appType}
          placeholder="Choose a type"
          onValueChange={(appType) => set({ appType })}
          className="w-full"
        />
      </Field>
      {chosenElsewhere ? (
        <p className="-mt-3 flex items-start gap-2 text-13 text-pretty text-fg">
          <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warn" />
          {`The commands above are ${typeName(inspection.app_type)}'s. Deployed as ${typeName(form.appType)}, the app is installed, built and started the ${typeName(form.appType)} way instead.`}
        </p>
      ) : null}

      <Group title="Address" description="Where the app answers, and how.">
        <div className="flex flex-col gap-5">
          <Field
            label="Domain"
            error={errors["domain"]}
            description="Other names, such as the www one, are added from the app's Domains tab once it is deployed."
          >
            <Input
              mono
              value={form.domain}
              onValueChange={(value: string) => set({ domain: value })}
              placeholder="app.example.com"
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              inputMode="url"
            />
          </Field>
          <Checkbox
            label="Serve it over HTTPS"
            description={`Orders a Let's Encrypt certificate for ${domain || "the domain"} once the site is up. The domain has to point to this server already.`}
            checked={form.ssl}
            onCheckedChange={(ssl) => set({ ssl })}
          />
          <div className="flex flex-col gap-1.5">
            <span aria-hidden="true" className="text-13 font-medium text-fg">
              Web server
            </span>
            <SegmentedControl<WebServer>
              label="Web server"
              options={[
                { value: "nginx", label: "nginx" },
                { value: "apache", label: "Apache" },
              ]}
              value={form.webserver}
              onValueChange={(webserver) => set({ webserver })}
              className="self-start"
            />
          </div>
        </div>
      </Group>

      <Group title="Runtime">
        <div className="flex flex-col gap-5">
          {hasPort(form.appType) ? (
            <Field
              label="Port"
              error={errors["port"]}
              description={portNote(inspection, taken)}
              className="sm:max-w-80"
            >
              <Input mono inputMode="numeric" value={form.port} onValueChange={(value: string) => set({ port: value })} autoComplete="off" />
            </Field>
          ) : null}
          <Choice<Layout>
            legend="Deploys"
            options={LAYOUTS}
            value={form.layout}
            onChange={(layout) => set({ layout })}
            badge={{ releases: "Recommended" }}
          />
          <p className="text-12 text-pretty text-fg-muted">Memory, CPU and task limits are set from the app's Settings once it exists.</p>
        </div>
      </Group>

      <Group
        title="Environment"
        description="Written to the app's .env before the first build. Change it later from the app's Environment tab."
      >
        <EnvironmentFields rows={form.env} errors={errors} onChange={(env) => set({ env })} />
      </Group>

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-6">
        <Button onClick={onBack}>Back</Button>
        <Button type="submit" variant="primary">
          Continue
        </Button>
      </div>
    </form>
  );
}
