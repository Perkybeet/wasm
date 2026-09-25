import { TriangleAlert } from "lucide-react";

import { StatusGlyph } from "../../components/ui/StatusPill";
import { cx } from "../../lib/cx";
import type { CertTone } from "./certificates";

const TONE_TEXT: Record<CertTone, string> = {
  ok: "text-ok",
  warn: "text-warn",
  fail: "text-fail",
  idle: "text-idle",
  busy: "text-warn",
};

/**
 * A certificate's state as glyph and words: a dot for valid, a warning sign for expiring or
 * not covering a name, a cross for expired, an arc while a job works on it, a ring for none.
 * The shape carries the state as much as the colour does.
 */
export function CertificateStatus({ tone, label, className }: { tone: CertTone; label: string; className?: string }) {
  return (
    <span data-tone={tone} className={cx("inline-flex min-w-0 items-center gap-1.5 text-13", className)}>
      <span className={cx("flex shrink-0", TONE_TEXT[tone])}>
        {tone === "warn" ? (
          <TriangleAlert aria-hidden="true" className="size-3.5" />
        ) : (
          <StatusGlyph
            state={tone === "ok" ? "running" : tone === "fail" ? "failed" : tone === "busy" ? "deploying" : "stopped"}
            size={12}
          />
        )}
      </span>
      <span className={cx("truncate", tone === "idle" ? "text-fg-muted" : "text-fg")}>{label}</span>
    </span>
  );
}
