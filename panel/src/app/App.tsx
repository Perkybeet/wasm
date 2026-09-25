import { CSPProvider } from "@base-ui/react/csp-provider";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";

import { isApiError } from "../api/client";
import { ToastProvider } from "../components/ui/Toast";
import { TooltipProvider } from "../components/ui/Tooltip";
import { ElevateDialog } from "../features/auth/ElevateDialog";
import { Announcer } from "./Announcer";
import { ErrorBoundary } from "./ErrorBoundary";
import type { AppRouter } from "./router";

/**
 * The console's query cache. A refusal (4xx) is an answer, not a glitch, so it is never
 * retried; an unreachable or failing server gets two more tries.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        retry: (failures, error) => {
          if (isApiError(error) && error.status >= 400 && error.status < 500) return false;
          return failures < 2;
        },
      },
      mutations: { retry: false },
    },
  });
}

/**
 * Providers shared by every page. The console runs under `style-src 'self'`, so Base UI is
 * told never to inject <style> elements; the one rule it would inject ships in app.css.
 */
export function App({ router, queryClient }: { router: AppRouter; queryClient: QueryClient }) {
  return (
    <CSPProvider disableStyleElements>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <ToastProvider>
            <ErrorBoundary>
              <RouterProvider router={router} />
            </ErrorBoundary>
            <ElevateDialog />
            <Announcer />
          </ToastProvider>
        </TooltipProvider>
      </QueryClientProvider>
    </CSPProvider>
  );
}
