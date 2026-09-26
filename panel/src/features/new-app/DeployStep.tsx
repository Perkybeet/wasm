import { useNavigate } from "@tanstack/react-router";
import { Rocket } from "lucide-react";
import { useEffect } from "react";
import type { Ref } from "react";

import type { FollowedJob } from "../../api/queries/jobs";
import type { KeyValueItem } from "../../components/page/KeyValueList";
import { KeyValueList } from "../../components/page/KeyValueList";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { Spinner } from "../../components/ui/Spinner";
import { normalizeDomain } from "../domains/names";
import { useDeploymentLanding } from "./useDeploymentLanding";
import type { LandingTarget } from "./useDeploymentLanding";
import { hasPort, typeName } from "./wizard";
import type { AppTypeOption, Inspection, ReviewForm, SourceForm } from "./wizard";

/** What is about to be deployed, one fact per row, as the operator reviewed it. */
export function deploySummary(source: SourceForm, inspection: Inspection, types: readonly AppTypeOption[], form: ReviewForm): KeyValueItem[] {
  const domain = normalizeDomain(form.domain);
  const variables = form.env.filter((row) => row.name.trim() !== "");
  const secrets = variables.filter((row) => row.secret).length;
  const branch = source.branch.trim() || inspection.branch;
  const revision = [branch ? `branch ${branch}` : null, inspection.commit ? `commit ${inspection.commit}` : null].filter(Boolean).join(", ");
  const paths = form.layout === "releases" ? form.persistentPaths.map((row) => row.value.trim()).filter((value) => value !== "") : [];
  return [
    { label: "Source", value: source.source.trim(), ...(revision ? { hint: revision } : {}) },
    {
      label: "Type",
      value: typeName(types, form.appType),
      mono: false,
      copy: false,
      hint: form.appType === inspection.app_type ? "As detected" : `Chosen over the detected ${typeName(types, inspection.app_type)}`,
    },
    {
      label: "Address",
      value: `${form.ssl ? "https" : "http"}://${domain}`,
      copy: `${form.ssl ? "https" : "http"}://${domain}`,
      ...(form.includeWww ? { hint: `www.${domain} redirects here` } : {}),
    },
    ...(hasPort(form.appType) ? [{ label: "Port", value: form.port.trim() }] : []),
    { label: "Web server", value: form.webserver === "apache" ? "Apache" : "nginx", mono: false, copy: false },
    {
      label: "Deploys",
      value: form.layout === "releases" ? "Releases, behind a health check" : "In place",
      mono: false,
      copy: false,
      ...(paths.length > 0 ? { hint: `Persistent: ${paths.join(", ")}` } : {}),
    },
    {
      label: "Environment",
      value:
        variables.length === 0
          ? "No variables"
          : `${String(variables.length)} ${variables.length === 1 ? "variable" : "variables"}${secrets > 0 ? `, ${String(secrets)} secret` : ""}`,
      mono: false,
      copy: false,
    },
  ];
}

/** After the deploy was queued: waiting for the deployer to record a deployment, then going to it. */
function Landing({ target, followedJob, onGone, onBack }: { target: LandingTarget; followedJob: FollowedJob; onGone: () => void; onBack: () => void }) {
  const navigate = useNavigate();
  const landing = useDeploymentLanding(target, followedJob);

  useEffect(() => {
    if (landing?.kind === "deployment") {
      onGone();
      void navigate({ to: "/apps/$domain/deployments/$id", params: { domain: target.domain, id: String(landing.id) }, replace: true });
    } else if (landing?.kind === "app") {
      onGone();
      void navigate({ to: "/apps/$domain", params: { domain: target.domain }, replace: true });
    }
  }, [landing, navigate, onGone, target.domain]);

  if (landing?.kind === "failed") {
    const job = followedJob.job;
    return (
      <div className="flex flex-col gap-3">
        <ErrorBlock
          live
          error={{ detail: job?.error ?? "The deploy failed before it started building, without saying why. Its log is on the Activity page." }}
          title={`The deploy of ${target.domain} failed before it started`}
          hint="Nothing was built. Fix what the error names, then deploy again."
        />
        <div>
          <Button onClick={onBack}>Back to review</Button>
        </div>
      </div>
    );
  }

  const step = followedJob.job?.current_step ?? null;
  return (
    <div role="status" className="flex min-w-0 flex-col gap-1 rounded-card border border-border bg-surface px-4 py-3 shadow-raised">
      <p className="flex items-center gap-2.5 text-14 font-medium text-fg">
        <Spinner size={16} className="text-warn" />
        {`Deploying ${target.domain}`}
      </p>
      <p className="flex min-w-0 flex-wrap items-center gap-x-2 text-13 text-fg-muted">
        <span>The build log opens as soon as the build starts.</span>
        {step ? (
          <code translate="no" className="min-w-0 truncate text-12" title={step}>
            {step}
          </code>
        ) : null}
      </p>
    </div>
  );
}

export interface DeployStepProps {
  source: SourceForm;
  inspection: Inspection;
  types: readonly AppTypeOption[];
  form: ReviewForm;
  onDeploy: () => void;
  deploying: boolean;
  /** The failure of the last attempt to queue it, when it is not about a field. */
  failure: unknown;
  /** Where the deploy `onDeploy` queued is followed to, once it exists. */
  target: LandingTarget | null;
  /** The job queued by `onDeploy`, followed for its status and its own failure. */
  followedJob: FollowedJob;
  onBack: () => void;
  /** Called just before the wizard navigates away for good. */
  onGone: () => void;
  headingRef: Ref<HTMLHeadingElement>;
}

/**
 * Step three: the summary, and the one button. Once queued, the wizard waits for the build to
 * start and hands over to its deployment page, where the log streams.
 */
export function DeployStep({ source, inspection, types, form, onDeploy, deploying, failure, target, followedJob, onBack, onGone, headingRef }: DeployStepProps) {
  const domain = normalizeDomain(form.domain);
  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h2 ref={headingRef} tabIndex={-1} className="title text-18 text-fg outline-none">
          Deploy
        </h2>
        <p className="text-14 text-pretty text-fg-muted">Nothing on this server has changed yet. Deploying fetches the source again and builds it.</p>
      </header>

      <div className="rounded-card border border-border bg-surface px-4 py-1 shadow-raised">
        <KeyValueList items={deploySummary(source, inspection, types, form)} />
      </div>

      {target !== null ? (
        <Landing key={target.jobId} target={target} followedJob={followedJob} onGone={onGone} onBack={onBack} />
      ) : (
        <>
          {failure !== null && failure !== undefined ? <ErrorBlock live error={failure} title={`${domain} was not deployed`} /> : null}
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-6">
            <Button disabled={deploying} onClick={onBack}>
              Back
            </Button>
            <Button variant="primary" size="lg" icon={<Rocket aria-hidden="true" />} loading={deploying} onClick={onDeploy}>
              {`Deploy ${domain}`}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
