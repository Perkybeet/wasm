// Checks a build for the "uPlot leaks into the eager chunk" regression: `_console/index.tsx`'s
// `validateSearch` used to import `WINDOWS` straight from `MachineCharts.tsx`, which imports
// `Chart.tsx`, which imports uplot - so importing one constant ran the whole module and dragged
// the charting library into the route's eagerly preloaded "route options" chunk instead of its
// lazily loaded component chunk.
//
// Never point this at the build committed at ../src/wasm/web/static: CLAUDE.md's packaging
// notes are explicit that only `git archive` ships it and that it is produced once, by CI, not
// rebuilt ad hoc. Build a private copy first (`npx vite build --outDir <dir> --emptyOutDir`),
// then:
//
//     node scripts/check-bundle-split.mjs <dir>

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

const dir = process.argv[2];
if (!dir) {
  console.error("usage: node scripts/check-bundle-split.mjs <build-dir>");
  process.exit(1);
}

const indexPath = path.join(dir, "index.html");
if (!existsSync(indexPath)) {
  console.error(`${indexPath} does not exist. Build first: npx vite build --outDir ${dir} --emptyOutDir`);
  process.exit(1);
}

const html = readFileSync(indexPath, "utf8");
const hrefs = [...html.matchAll(/rel="modulepreload"[^>]*href="([^"]+)"/g)]
  .map((match) => match[1])
  .filter((href) => href !== undefined);
if (hrefs.length < 10) {
  console.error(`Expected several modulepreload links in ${indexPath}, found ${String(hrefs.length)}.`);
  process.exit(1);
}

const offenders = hrefs.filter((href) => {
  const file = path.join(dir, href.replace(/^\//, ""));
  return /uplot/i.test(readFileSync(file, "utf8"));
});

if (offenders.length > 0) {
  console.error("uPlot leaked into an eagerly preloaded chunk:");
  for (const href of offenders) console.error(`  ${href}`);
  process.exit(1);
}

console.log(`No uPlot code in any of the ${String(hrefs.length)} eagerly modulepreloaded chunks.`);
