import { CSPProvider } from "@base-ui/react/csp-provider";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";

import { ToastProvider, TooltipProvider } from "../components/ui";
import type { AppRouter } from "./router";

const queryClient = new QueryClient();

/**
 * Providers shared by every page. The console runs under `style-src 'self'`, so Base UI is
 * told never to inject <style> elements; the one rule it would inject ships in app.css.
 */
export function App({ router }: { router: AppRouter }) {
  return (
    <CSPProvider disableStyleElements>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <ToastProvider>
            <RouterProvider router={router} />
          </ToastProvider>
        </TooltipProvider>
      </QueryClientProvider>
    </CSPProvider>
  );
}
