/**
 * Whether a value is safe to use as a navigable `href`: an absolute http or https URL. A
 * relative path, a bare hostname, or a scheme built to run script or leave the browser
 * (`javascript:`, `data:`, `vbscript:`...) is not - and an app's source, a chart marker's
 * link and an update's release notes are all, one way or another, a string the server or a
 * third party sent, not one this code wrote itself.
 */
export function isHttpUrl(value: string): boolean {
  return value.startsWith("http://") || value.startsWith("https://");
}
