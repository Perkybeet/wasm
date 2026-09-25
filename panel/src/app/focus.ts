/**
 * Moves focus to the page's h1 after a navigation, so a screen reader announces the new page
 * and the next Tab starts from its top. Left alone when focus sits in a `data-keep-focus`
 * region (a tab bar the operator is moving along).
 */
export function focusPageTitle(): void {
  const active = document.activeElement;
  if (active instanceof HTMLElement && active !== document.body && active.closest("[data-keep-focus]")) return;
  document.querySelector<HTMLElement>("main [data-page-title]")?.focus({ preventScroll: true });
}
