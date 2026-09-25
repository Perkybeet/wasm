import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useBlocker } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { request } from "../../api/client";
import { appKeys, appsQuery } from "../../api/queries/apps";
import { webserverQuery } from "../../api/queries/config";
import { deploymentsQuery } from "../../api/queries/deployments";
import { jobKeys } from "../../api/queries/jobs";
import type { Job } from "../../api/queries/jobs";
import { announce } from "../../app/Announcer";
import { PageHeader } from "../../app/PageHeader";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { normalizeDomain } from "../domains/names";
import { DeployStep } from "./DeployStep";
import { ReviewStep } from "./ReviewStep";
import { SourceStep } from "./SourceStep";
import { StepRail } from "./StepRail";
import type { LandingTarget } from "./useDeploymentLanding";
import { STEPS, createAppBody, initialReview, manualInspection, refusalOf, reviewProblems, shortSource, sourceKind, sourceProblems } from "./wizard";
import type { Inspection, ReviewErrors, ReviewForm, SourceErrors, SourceForm, Step, WebServer } from "./wizard";

const BREADCRUMBS = [{ label: "Applications", to: "/apps" }] as const;

function sameSource(a: SourceForm, b: SourceForm): boolean {
  return a.source.trim() === b.source.trim() && a.branch.trim() === b.branch.trim();
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/**
 * The new-app wizard: where the code is, what WASM found in it (every part editable), and the
 * deploy, which hands over to the deployment page as soon as the build starts. What the
 * operator typed survives going back and forth between the steps and is never asked twice.
 */
export function NewAppWizard() {
  const queryClient = useQueryClient();
  const apps = useQuery(appsQuery());
  const webserver = useQuery(webserverQuery());

  const [step, setStep] = useState<Step>("source");
  const [source, setSource] = useState<SourceForm>({ source: "", branch: "" });
  const [sourceErrors, setSourceErrors] = useState<SourceErrors>({});
  const [inspected, setInspected] = useState<{ inspection: Inspection; for: SourceForm } | null>(null);
  const [review, setReview] = useState<ReviewForm | null>(null);
  const [reviewErrors, setReviewErrors] = useState<ReviewErrors>({});
  const [inspectingSince, setInspectingSince] = useState<number | null>(null);
  const [target, setTarget] = useState<LandingTarget | null>(null);

  const heading = useRef<HTMLHeadingElement>(null);
  const moved = useRef(false);
  const leaving = useRef(false);
  const abort = useRef<AbortController | null>(null);

  const go = (next: Step): void => {
    moved.current = true;
    setStep(next);
    const index = STEPS.findIndex((entry) => entry.id === next);
    announce(`Step ${String(index + 1)} of ${String(STEPS.length)}: ${STEPS[index]?.label ?? next}`);
  };

  // A new step starts at its heading, so the next Tab and a screen reader begin there.
  useEffect(() => {
    if (!moved.current) return;
    heading.current?.focus({ preventScroll: false });
    heading.current?.scrollIntoView({ block: "nearest" });
  }, [step]);

  const context = useMemo(() => {
    const list = apps.data?.apps ?? [];
    return {
      domains: new Set(list.map((app) => app.domain)),
      ports: new Map(list.filter((app) => app.port !== null && app.port !== undefined).map((app) => [Number(app.port), app.domain])),
    };
  }, [apps.data]);

  const defaultWebserver: WebServer = webserver.data?.webserver === "apache" ? "apache" : "nginx";

  const inspect = useMutation({
    mutationFn: async (form: SourceForm) => {
      abort.current?.abort();
      const controller = new AbortController();
      abort.current = controller;
      const branch = form.branch.trim();
      const inspection = await request("post", "/api/apps/inspect", {
        body: { source: form.source.trim(), ...(branch !== "" && sourceKind(form.source) !== "local" ? { branch } : {}) },
        signal: controller.signal,
      });
      return { inspection, form };
    },
    onMutate: () => {
      setInspectingSince(Date.now());
    },
    onSettled: () => {
      setInspectingSince(null);
    },
    onSuccess: ({ inspection, form }) => {
      setInspected({ inspection, for: form });
      setReview((previous) => initialReview(inspection, { webserver: defaultWebserver, taken: context.ports }, previous));
      setReviewErrors({});
      go("review");
    },
    onError: (error) => {
      const refusal = refusalOf(error);
      if (refusal?.step === "source" && Object.keys(refusal.fields).length > 0) setSourceErrors(refusal.fields);
    },
  });

  const create = useMutation({
    mutationFn: async ({ form, body }: { form: ReviewForm; body: ReturnType<typeof createAppBody> }) => {
      // The newest deployment of this domain before the deploy, so the deploy's own row is
      // recognised as the first one after it (a deleted app's history can linger).
      const before = await queryClient.query({ ...deploymentsQuery({ domain: body.domain, limit: 1 }), staleTime: 0 });
      const accepted = await request("post", "/api/apps", { body });
      return { accepted, after: before.items[0]?.id ?? 0, form };
    },
    onSuccess: ({ accepted, after }, { body }) => {
      queryClient.setQueryData<Job>(jobKeys.detail(accepted.job_id), (current) => current ?? (accepted.job as unknown as Job));
      void queryClient.invalidateQueries({ queryKey: jobKeys.active });
      void queryClient.invalidateQueries({ queryKey: appKeys.list, exact: true });
      announce(`Deploy of ${body.domain} queued`);
      setTarget({ domain: body.domain, jobId: accepted.job_id, after });
    },
    onError: (error) => {
      const refusal = refusalOf(error);
      if (refusal === null) return;
      if (refusal.step === "source") {
        setSourceErrors(refusal.fields);
        go("source");
      } else if (Object.keys(refusal.fields).length > 0) {
        setReviewErrors(refusal.fields);
        go("review");
      }
    },
  });

  const dirty = source.source.trim() !== "" && target === null;
  const blocker = useBlocker({
    // Signing in again after the session expired is not leaving: nothing typed could be kept.
    shouldBlockFn: ({ next }) => dirty && !leaving.current && next.pathname !== "/login",
    enableBeforeUnload: () => dirty && !leaving.current,
    withResolver: true,
  });
  const onGone = useCallback(() => {
    leaving.current = true;
  }, []);

  const submitSource = (): void => {
    const errors = sourceProblems(source);
    setSourceErrors(errors);
    if (Object.keys(errors).length > 0) return;
    if (inspected !== null && sameSource(inspected.for, source) && review !== null) {
      go("review");
      return;
    }
    inspect.mutate(source);
  };

  const submitReview = (): void => {
    if (review === null) return;
    const errors = reviewProblems(review, context);
    setReviewErrors(errors);
    if (Object.keys(errors).length > 0) {
      // The first field that needs attention takes focus; its message is read with it.
      requestAnimationFrame(() => {
        document.querySelector<HTMLElement>("main form [aria-invalid='true']")?.focus();
      });
      return;
    }
    create.reset();
    go("deploy");
  };

  const deploy = (): void => {
    if (review === null || inspected === null) return;
    create.mutate({ form: review, body: createAppBody(inspected.for, review) });
  };

  const notes: Partial<Record<Step, string>> = {
    source: shortSource(inspected?.for.source ?? source.source),
    review: review ? normalizeDomain(review.domain) : "",
  };

  const createFailure = create.isError && refusalOf(create.error) === null ? create.error : null;

  return (
    <>
      <PageHeader
        title="New application"
        description="Deploy from a Git repository or a directory on this server."
        breadcrumbs={BREADCRUMBS}
      />
      <div className="grid min-w-0 gap-6 lg:grid-cols-[13rem_minmax(0,1fr)] lg:gap-10">
        <div className="min-w-0 lg:sticky lg:top-20 lg:self-start">
          <StepRail
            current={step}
            notes={notes}
            locked={inspect.isPending || create.isPending || target !== null}
            onGoTo={(next) => {
              go(next);
            }}
          />
        </div>
        <div className="min-w-0 max-w-[46rem]">
          {step === "source" ? (
            <SourceStep
              form={source}
              errors={sourceErrors}
              onChange={(next) => {
                setSource(next);
                setSourceErrors({});
                if (inspect.isError) inspect.reset();
              }}
              onSubmit={submitSource}
              inspecting={inspectingSince === null ? null : { since: inspectingSince }}
              onCancel={() => {
                abort.current?.abort();
                inspect.reset();
              }}
              failure={inspect.isError && !isAbort(inspect.error) ? inspect.error : null}
              inspected={inspected !== null && sameSource(inspected.for, source)}
              onInspectAgain={() => inspect.mutate(source)}
              onManual={() => {
                const inspection = manualInspection(source);
                inspect.reset();
                setInspected({ inspection, for: source });
                setReview((previous) => initialReview(inspection, { webserver: defaultWebserver, taken: context.ports }, previous));
                setReviewErrors({});
                go("review");
              }}
              headingRef={heading}
            />
          ) : null}
          {step === "review" && inspected !== null && review !== null ? (
            <ReviewStep
              taken={context.ports}
              inspection={inspected.inspection}
              source={inspected.for.source.trim()}
              form={review}
              errors={reviewErrors}
              onChange={(next) => {
                setReview(next);
                if (Object.keys(reviewErrors).length > 0) setReviewErrors(reviewProblems(next, context));
              }}
              onBack={() => go("source")}
              onContinue={submitReview}
              headingRef={heading}
            />
          ) : null}
          {step === "deploy" && inspected !== null && review !== null ? (
            <DeployStep
              source={inspected.for}
              inspection={inspected.inspection}
              form={review}
              onDeploy={deploy}
              deploying={create.isPending}
              failure={createFailure}
              target={target}
              onBack={() => {
                setTarget(null);
                create.reset();
                go("review");
              }}
              onGone={onGone}
              headingRef={heading}
            />
          ) : null}
        </div>
      </div>

      <Dialog
        open={blocker.status === "blocked"}
        onOpenChange={(open) => {
          if (!open) blocker.reset?.();
        }}
        size="sm"
        title="Leave the new application?"
        description="Nothing has been deployed, and what you filled in is not kept."
        footer={
          <>
            <Button onClick={() => blocker.reset?.()}>Stay</Button>
            <Button variant="danger" onClick={() => blocker.proceed?.()}>
              Leave
            </Button>
          </>
        }
      />
    </>
  );
}
