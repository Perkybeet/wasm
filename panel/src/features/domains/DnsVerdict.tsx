import { Check, TriangleAlert, X } from "lucide-react";

import type { DnsCheck } from "../../api/queries/domains";
import { StatusGlyph } from "../../components/ui/StatusPill";
import { cx } from "../../lib/cx";
import { dnsVerdict, isPrivateAddress, recordsToCreate } from "./dns";

function AddressList({ label, addresses, matches, empty }: { label: string; addresses: readonly string[]; matches?: ReadonlySet<string>; empty: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <p className="text-12 text-fg-muted">{label}</p>
      {addresses.length === 0 ? (
        <p className="text-13 text-fg-faint">{empty}</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {addresses.map((address) => {
            const matched = matches?.has(address);
            return (
              <li key={address} className="flex min-w-0 items-center gap-1.5">
                {matches === undefined ? null : matched ? (
                  <Check aria-hidden="true" className="size-3.5 shrink-0 text-ok" />
                ) : (
                  <X aria-hidden="true" className="size-3.5 shrink-0 text-fail" />
                )}
                <code translate="no" className="truncate text-12 text-fg">
                  {address}
                </code>
                {matches === undefined ? null : (
                  <span className="sr-only">{matched ? " (this server)" : " (not this server)"}</span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/**
 * The answer to "does this name point here?": a verdict in words and shape, this server's
 * addresses beside the ones the name resolves to, and the records to create when it does not.
 */
export function DnsVerdict({ check, className }: { check: DnsCheck; className?: string }) {
  const verdict = dnsVerdict(check);
  const expected = new Set(check.expected_addresses);
  const records = recordsToCreate(check.expected_addresses);
  const onlyPrivate = check.expected_addresses.length > 0 && check.expected_addresses.every(isPrivateAddress);
  const heading =
    verdict === "here"
      ? `${check.domain} points here`
      : verdict === "missing"
        ? `${check.domain} has no DNS record yet`
        : `${check.domain} points somewhere else`;

  return (
    <div
      data-verdict={verdict}
      className={cx(
        "flex min-w-0 flex-col gap-3 rounded-card border p-3",
        verdict === "here" ? "border-ok/30 bg-ok-soft/40" : "border-warn/30 bg-warn-soft/40",
        className,
      )}
    >
      <p className="flex items-center gap-2 text-14 font-medium text-fg">
        {verdict === "here" ? (
          <StatusGlyph state="running" className="text-ok" />
        ) : (
          <TriangleAlert aria-hidden="true" className="size-4 shrink-0 text-warn" />
        )}
        <span className="min-w-0 break-words">{heading}</span>
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <AddressList label="This server" addresses={check.expected_addresses} empty="No public address found" />
        <AddressList
          label={`${check.domain} resolves to`}
          addresses={check.resolved_addresses}
          matches={expected}
          empty="Nothing: no A or AAAA record"
        />
      </div>
      {verdict !== "here" ? (
        <p className="text-13 text-pretty text-fg-muted">
          {records.length > 0 ? (
            <>
              {`At your DNS provider, point ${check.domain} here with `}
              {records.map((record, index) => (
                <span key={record.value}>
                  {index > 0 ? " and " : null}
                  {`an ${record.type} record to `}
                  <code translate="no" className="text-12 text-fg">
                    {record.value}
                  </code>
                </span>
              ))}
              {", then check again. DNS changes can take a while to reach everyone."}
            </>
          ) : (
            "DNS changes can take a while to reach everyone."
          )}
          {onlyPrivate
            ? " This server only sees private addresses: behind NAT, a record pointing at its public address still reads as elsewhere, and the certificate order is what proves it."
            : null}
        </p>
      ) : null}
    </div>
  );
}
