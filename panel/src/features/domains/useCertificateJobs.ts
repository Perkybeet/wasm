import { useQueryClient } from "@tanstack/react-query";

import { certKeys } from "../../api/queries/certs";
import { isJobFinished } from "../../api/queries/jobs";
import { siteKeys } from "../../api/queries/sites";
import { useServerEvent } from "../../realtime/events";
import { CERT_JOB_TYPES } from "./certificates";

/**
 * Refreshes the certificates and the sites whenever a certificate job ends, whoever started
 * it: issuing, expanding and renewing change what certbot lists and what a site serves.
 */
export function useCertificateRefresh(): void {
  const queryClient = useQueryClient();
  useServerEvent("job", (job) => {
    if (!CERT_JOB_TYPES.has(job.type) || !isJobFinished(job)) return;
    void queryClient.invalidateQueries({ queryKey: certKeys.all });
    void queryClient.invalidateQueries({ queryKey: certKeys.details });
    void queryClient.invalidateQueries({ queryKey: siteKeys.all });
    void queryClient.invalidateQueries({ queryKey: siteKeys.details });
  });
}
