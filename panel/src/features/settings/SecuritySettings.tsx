import { useQuery } from "@tanstack/react-query";

import { sessionsQuery } from "../../api/queries/auth";
import { configQuery } from "../../api/queries/config";
import { useDocumentTitle } from "../../app/documentTitle";
import { KeyValueList, KeyValueListSkeleton } from "../../components/page/KeyValueList";
import type { KeyValueItem } from "../../components/page/KeyValueList";
import { QueryState } from "../../components/page/QueryState";
import { Sections } from "../../components/page/Section";
import { formatCount } from "../../lib/format";
import { perWindow, readLockoutPolicy, spokenDuration } from "./security";
import type { LockoutPolicy } from "./security";
import { SessionsSection } from "./SessionsSection";
import { SettingsSection } from "./SettingsForm";
import { TwoFactorSection } from "./TwoFactorSection";

function policyItems(policy: LockoutPolicy): KeyValueItem[] {
  const unknown = "Not set";
  return [
    {
      label: "Failed sign-ins before lockout",
      value: policy.maxFailedAttempts === null ? unknown : formatCount(policy.maxFailedAttempts),
      copy: false,
      mono: false,
      hint: "Per client address. Wrong codes when turning two-factor off count too.",
    },
    {
      label: "Lockout lasts",
      value: policy.lockoutSeconds === null ? unknown : spokenDuration(policy.lockoutSeconds),
      copy: false,
      mono: false,
    },
    {
      label: "Request limit",
      value: !policy.rateLimitEnabled
        ? "Off"
        : policy.rateLimitRequests === null || policy.rateLimitWindowSeconds === null
          ? unknown
          : `${formatCount(policy.rateLimitRequests)} requests ${perWindow(policy.rateLimitWindowSeconds)}`,
      copy: false,
      mono: false,
      ...(policy.rateLimitEnabled ? { hint: "Counted per client address." } : {}),
    },
    {
      label: "Session lifetime",
      value: policy.sessionHours === null ? unknown : spokenDuration(policy.sessionHours * 3600),
      copy: false,
      mono: false,
      hint: "Renewed while the session is in use.",
    },
    {
      label: "Allowed addresses",
      value: policy.ipAllowlist.length === 0 ? "Any address" : policy.ipAllowlist.join(", "),
      mono: policy.ipAllowlist.length > 0,
      copy: policy.ipAllowlist.length === 0 ? false : policy.ipAllowlist.join(", "),
    },
  ];
}

/** The lockout and rate limits, as configured. Read-only: they are edited in config.yaml. */
function LockoutSection() {
  const query = useQuery(configQuery());
  return (
    <SettingsSection
      title="Lockout policy"
      description="How the console answers repeated failed sign-ins. Set under web in config.yaml and read when the console starts, so a change applies after a restart."
      commands={["wasm config get web", "wasm web restart"]}
    >
      <QueryState query={query} label="the lockout policy" skeleton={
          <div className="rounded-card border border-border bg-surface px-5 py-2 shadow-raised">
            <KeyValueListSkeleton rows={5} hints={[0, 2, 3]} />
          </div>
        }
      >
        {(data) => (
          <div className="rounded-card border border-border bg-surface px-5 py-2 shadow-raised">
            <KeyValueList items={policyItems(readLockoutPolicy(data.config))} />
          </div>
        )}
      </QueryState>
    </SettingsSection>
  );
}

/** Settings > Security: the second factor, who is signed in, and the lockout policy. */
export function SecuritySettings() {
  useDocumentTitle("Security settings", 1);
  const sessions = useQuery(sessionsQuery());
  return (
    <Sections>
      <TwoFactorSection />
      <SessionsSection />
      {/* After the sessions, whose number is not known until they load: drawn with them, the
          policy never jumps down the page as the list lengthens above it. */}
      {sessions.data !== undefined || sessions.isError ? <LockoutSection /> : null}
    </Sections>
  );
}
