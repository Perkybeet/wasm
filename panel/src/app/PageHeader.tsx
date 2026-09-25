import { Link } from "@tanstack/react-router";
import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";

import { useDocumentTitle } from "./documentTitle";

export interface Breadcrumb {
  label: string;
  /** A path of the console, with params already filled in. */
  to: string;
}

export interface PageHeaderProps {
  title: string;
  description?: ReactNode;
  /** The page's primary actions, right-aligned; the most important last. */
  actions?: ReactNode;
  breadcrumbs?: readonly Breadcrumb[];
}

/**
 * The start of every page: where you are, what this page is for, and what you can do here.
 * The h1 takes focus after a navigation, so a screen reader hears the new page's name first.
 */
export function PageHeader({ title, description, actions, breadcrumbs }: PageHeaderProps) {
  useDocumentTitle(title);
  return (
    <header className="mb-8 flex flex-wrap items-end justify-between gap-x-6 gap-y-4">
      <div className="min-w-0 flex-1 basis-80">
        {breadcrumbs && breadcrumbs.length > 0 ? (
          <nav aria-label="Breadcrumb" className="mb-2">
            <ol className="flex flex-wrap items-center gap-1 text-13 text-fg-muted">
              {breadcrumbs.map((crumb) => (
                <li key={crumb.to} className="flex items-center gap-1">
                  <Link
                    to={crumb.to}
                    className="rounded-[4px] hover:text-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus"
                  >
                    {crumb.label}
                  </Link>
                  <ChevronRight aria-hidden="true" className="size-3.5 text-fg-faint" />
                </li>
              ))}
            </ol>
          </nav>
        ) : null}
        <h1
          tabIndex={-1}
          data-page-title=""
          className="title text-24 break-words text-fg outline-none focus-visible:rounded-[4px] focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-focus"
        >
          {title}
        </h1>
        {description !== undefined ? (
          <p className="mt-1.5 max-w-[68ch] text-14 text-pretty text-fg-muted">{description}</p>
        ) : null}
      </div>
      {actions !== undefined ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}
