import { X } from "lucide-react";

import { ErrorBlock } from "../../components/page/QueryState";
import { IconButton } from "../../components/ui/IconButton";
import { Spinner } from "../../components/ui/Spinner";
import { CertificateStatus } from "./CertificateStatus";
import { isJobFinished } from "../../api/queries/jobs";
import type { FollowedJob } from "../../api/queries/jobs";

export interface JobWords {
  /** While it runs: "Extending the certificate to shop.example.com". */
  running: string;
  /** Once it succeeded: "The certificate covers every domain". */
  done: string;
  /** Once it failed: "The certificate was not extended". */
  failed: string;
  /** What to do about a failure, above the job's own words. */
  hint?: string;
}

/**
 * A job this page queued, followed where the operator started it: its current step while it
 * runs, a quiet confirmation when it succeeds, and its error verbatim when it fails. Both
 * outcomes stay until dismissed; the live region says each once.
 */
export function JobBanner({ followed, words }: { followed: FollowedJob; words: JobWords }) {
  const job = followed.job;
  if (followed.id === null) return null;
  if (job === null || !isJobFinished(job)) {
    const step = job?.current_step ?? null;
    return (
      <div
        role="status"
        className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1 rounded-control border border-border bg-surface px-3 py-2 text-13 shadow-raised"
      >
        <Spinner size={14} className="text-warn" />
        <span className="font-medium text-fg">{words.running}</span>
        {step ? (
          <code translate="no" className="min-w-0 truncate text-12 text-fg-muted" title={step}>
            {step}
          </code>
        ) : null}
      </div>
    );
  }
  if (job.status === "completed") {
    return (
      <div
        role="status"
        className="flex items-center justify-between gap-3 rounded-control border border-ok/30 bg-ok-soft/40 py-1.5 pr-1.5 pl-3 text-13"
      >
        <CertificateStatus tone="ok" label={words.done} />
        <IconButton label="Dismiss" icon={<X />} size="sm" onClick={followed.dismiss} />
      </div>
    );
  }
  return (
    <div className="relative">
      <ErrorBlock
        live
        error={{
          detail:
            job.error ??
            (job.status === "cancelled" ? "The job was cancelled." : "The job failed without saying why. Its log is on the Activity page."),
        }}
        title={words.failed}
        {...(words.hint !== undefined ? { hint: words.hint } : {})}
        className="pr-12"
      />
      <IconButton label="Dismiss" icon={<X />} size="sm" onClick={followed.dismiss} className="absolute top-2.5 right-2.5" />
    </div>
  );
}
