import { Archive, FolderGit2, FolderOpen, GitBranch, Search, X } from "lucide-react";
import { useRef } from "react";
import type { ReactNode, Ref, SyntheticEvent } from "react";

import { CommandHint } from "../../components/page/CommandHint";
import { isApiError } from "../../api/client";
import { ErrorBlock } from "../../components/page/QueryState";
import { useNow } from "../../components/page/clock";
import { Button } from "../../components/ui/Button";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { Skeleton } from "../../components/ui/Skeleton";
import { Spinner } from "../../components/ui/Spinner";
import { useKeyboardScrollable } from "../domains/useKeyboardScrollable";
import { SOURCE_WORDS, sourceKind } from "./wizard";
import type { SourceErrors, SourceForm, SourceKind } from "./wizard";

const KIND_ICON: Record<SourceKind, ReactNode> = {
  git: <GitBranch />,
  archive: <Archive />,
  local: <FolderOpen />,
  unknown: <FolderGit2 />,
};

/** How long the inspection has been running, in whole seconds, so a slow clone reads as alive. */
function Elapsed({ since }: { since: number }) {
  const now = useNow(() => 1000);
  const seconds = Math.max(0, Math.floor((now - since) / 1000));
  return <span className="mono text-12 text-fg-faint">{`${String(seconds)}s`}</span>;
}

/** The shape of the Review step, while the source is fetched and read. */
function ReviewSkeleton() {
  return (
    <div aria-hidden="true" className="flex flex-col gap-4 rounded-card border border-border bg-surface p-4 shadow-raised">
      <div className="flex items-center gap-3">
        <Skeleton className="h-4 w-4 rounded-[4px]" />
        <Skeleton className="h-3 w-64 max-w-full" />
      </div>
      <div className="flex flex-col gap-2.5 rounded-control bg-bg-sunken p-3">
        {["w-32", "w-44", "w-36"].map((width) => (
          <div key={width} className="flex items-center gap-4">
            <Skeleton className="h-3 w-12" />
            <Skeleton className={`h-3 ${width}`} />
          </div>
        ))}
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Skeleton className="h-8" />
        <Skeleton className="h-8" />
      </div>
    </div>
  );
}

const FETCH_HINT = "Check the address and that this server can reach it. A private repository needs a deploy key, or a token in the URL.";

/**
 * Why the inspection failed. A source that could not be fetched is marked on its field, and
 * what the fetch printed (git's own words) is shown here verbatim. A source that was fetched
 * but matched no type can still be deployed: the operator picks the type.
 */
function InspectFailure({ failure, source, onManual }: { failure: unknown; source: string; onManual: () => void }) {
  const output = useRef<HTMLDivElement>(null);
  useKeyboardScrollable(output);
  let block: ReactNode;
  if (isApiError(failure) && failure.error === "sourceerror") {
    if (failure.hint === null) return null;
    block = <ErrorBlock live error={{ detail: failure.hint }} title={`What fetching ${source} reported`} hint={FETCH_HINT} />;
  } else if (isApiError(failure) && failure.error === "deploymenterror") {
    block = (
      <>
        <ErrorBlock live error={failure} title={`WASM could not tell what ${source} is`} />
        <div>
          <Button onClick={onManual}>Choose the type yourself</Button>
        </div>
      </>
    );
  } else {
    block = <ErrorBlock live error={failure} title={`Could not inspect ${source}`} hint={FETCH_HINT} />;
  }
  return (
    <div ref={output} className="flex flex-col gap-3">
      {block}
    </div>
  );
}

export interface SourceStepProps {
  form: SourceForm;
  errors: SourceErrors;
  onChange: (form: SourceForm) => void;
  onSubmit: () => void;
  /** The inspection in flight, and when it started. */
  inspecting: { since: number } | null;
  onCancel: () => void;
  /** Why the last inspection failed, verbatim. */
  failure: unknown;
  /** An inspection of exactly this source is already in hand: continuing needs no new one. */
  inspected: boolean;
  onInspectAgain: () => void;
  /** Go on without detection: the operator chooses the type. */
  onManual: () => void;
  headingRef: Ref<HTMLHeadingElement>;
}

/**
 * Step one: where the code is. WASM fetches it into a throwaway checkout and reads it, so the
 * next step proposes real commands, a real port and the variables the project declares.
 */
export function SourceStep({
  form,
  errors,
  onChange,
  onSubmit,
  inspecting,
  onCancel,
  failure,
  inspected,
  onInspectAgain,
  onManual,
  headingRef,
}: SourceStepProps) {
  const kind = sourceKind(form.source);
  const local = kind === "local";
  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    onSubmit();
  };
  const busy = inspecting !== null;
  const shown = form.source.trim();

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h2 ref={headingRef} tabIndex={-1} className="title text-18 text-fg outline-none">
          Source
        </h2>
        <p className="text-14 text-pretty text-fg-muted">
          WASM fetches it into a throwaway directory and reads it: nothing is installed or deployed yet.
        </p>
      </header>

      <Field label="Repository or directory" error={errors.source} description={SOURCE_WORDS[kind]}>
        <Input
          mono
          icon={KIND_ICON[kind]}
          value={form.source}
          onValueChange={(value: string) => onChange({ ...form, source: value })}
          placeholder="https://github.com/you/app.git"
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          disabled={busy}
        />
      </Field>

      {local ? null : (
        <Field
          label="Branch"
          optional
          error={errors.branch}
          description="Empty deploys the repository's default branch. Pushes to this branch can redeploy it later."
          className="sm:max-w-80"
        >
          <Input
            mono
            icon={<GitBranch />}
            value={form.branch}
            onValueChange={(value: string) => onChange({ ...form, branch: value })}
            placeholder="main"
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            disabled={busy}
          />
        </Field>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {inspected && !busy ? (
          <>
            <Button type="submit" variant="primary">
              Continue
            </Button>
            <Button variant="ghost" onClick={onInspectAgain}>
              Inspect again
            </Button>
          </>
        ) : (
          <Button type="submit" variant="primary" icon={<Search aria-hidden="true" />} loading={busy}>
            Inspect source
          </Button>
        )}
        {busy ? (
          <Button variant="ghost" icon={<X aria-hidden="true" />} onClick={onCancel}>
            Cancel
          </Button>
        ) : null}
      </div>

      {busy ? (
        <div className="flex flex-col gap-3">
          <p role="status" className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1 text-13 text-fg">
            <Spinner size={14} className="text-warn" />
            <span>{local ? "Copying" : kind === "archive" ? "Downloading" : "Cloning"}</span>
            <code translate="no" className="min-w-0 truncate text-12 text-fg-muted" title={shown}>
              {shown}
            </code>
            {!local && form.branch.trim() !== "" ? <span className="text-fg-muted">{`at ${form.branch.trim()}`}</span> : null}
            <Elapsed since={inspecting.since} />
          </p>
          <ReviewSkeleton />
        </div>
      ) : failure !== null && failure !== undefined ? (
        <InspectFailure failure={failure} source={shown} onManual={onManual} />
      ) : null}

      <CommandHint command={`wasm create --domain example.com --source ${shown === "" ? "https://github.com/you/app.git" : shown}`} label="From a terminal" />
    </form>
  );
}
