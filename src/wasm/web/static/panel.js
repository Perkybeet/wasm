/*
 * Panel behaviour that htmx cannot express.
 *
 * There is deliberately very little here. Navigation, forms and partial
 * updates are hypermedia; this file covers the four things that genuinely
 * need a client: a terminal for streaming logs, the live event feed, notices
 * and problems that outlive a swap, and the theme toggle.
 *
 * No build step. This is served as written.
 *
 * **This is a classic script, and it is loaded before Alpine.** Both matter.
 * Alpine starts itself from a microtask queued as it loads, and microtasks
 * queued by one deferred script are flushed before the next one runs, so with
 * Alpine first this file had not executed yet when x-data="logDrawer()" was
 * evaluated. Nothing reported that: the component silently failed to
 * initialise, window.wasmPanel was never assigned, and the optional chain in
 * the delegated click handler turned every Logs button in the panel into a
 * no-op. A module cannot be ordered before Alpine, which is why the exports
 * are gone.
 */

/** Maps a resource state to the class the stylesheet expects. */
const STATE_CLASS = {
  active: "active",
  running: "active",
  valid: "active",
  failed: "failed",
  error: "failed",
  expired: "failed",
  busy: "busy",
  deploying: "busy",
};

/** How tall the open drawer is, as the stylesheet lays it out. */
const DRAWER_HEIGHT = "18.5rem";

/* ------------------------------------------------------------------ theme */

const THEME_KEY = "wasm.theme";

/**
 * Apply a theme and remember it.
 *
 * @param {"light"|"dark"|null} theme Theme to apply, or null to follow the
 *   operating system.
 */
function setTheme(theme) {
  if (theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  } else {
    delete document.documentElement.dataset.theme;
    localStorage.removeItem(THEME_KEY);
  }
}

/**
 * Switch between light and dark, from whatever is on screen now.
 */
function toggleTheme() {
  const current =
    document.documentElement.dataset.theme ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  setTheme(current === "dark" ? "light" : "dark");
}

const storedTheme = localStorage.getItem(THEME_KEY);
if (storedTheme) {
  document.documentElement.dataset.theme = storedTheme;
}

/* ---------------------------------------------------------------- notices */

/**
 * Show a transient message.
 *
 * The wording rule: an action keeps its name through the whole flow, so the
 * button that says "Restart" produces "Restarted".
 *
 * @param {string} text Message to show.
 * @param {"active"|"failed"|"idle"} state Which rail colour to use.
 * @param {number} ttl Milliseconds before it disappears.
 */
function notify(text, state = "idle", ttl = 5000) {
  const host = document.getElementById("notices");
  if (!host) return;

  const item = document.createElement("div");
  item.className = `notice__item notice__item--${STATE_CLASS[state] || "idle"}`;
  item.textContent = text;
  host.append(item);

  window.setTimeout(() => item.remove(), ttl);
}

/**
 * Show a system error the way the design direction requires it.
 *
 * A message from nginx, systemd or certbot is never paraphrased and never
 * truncated: the fix goes above it in plain words, the tool's own output goes
 * below it verbatim in mono, and it stays on screen until it is dismissed,
 * because it is the thing the operator is about to search the web for. It used
 * to be 300 characters of raw JSON in the body font, in a toast that vanished
 * after ten seconds.
 *
 * @param {string} fix What to do about it.
 * @param {string} output The tool's own output, verbatim.
 */
function showProblem(fix, output) {
  const page = document.querySelector(".page");
  if (!page) {
    notify(fix, "failed", 10000);
    return;
  }

  document.getElementById("live-problem")?.remove();

  const problem = document.createElement("div");
  problem.className = "problem";
  problem.id = "live-problem";
  problem.setAttribute("role", "alert");

  const line = document.createElement("p");
  line.className = "problem__fix";
  line.textContent = fix;
  problem.append(line);

  if (output) {
    const pre = document.createElement("pre");
    pre.className = "problem__output";
    // textContent, never innerHTML: this string is the unedited output of a
    // tool, and a domain name inside it can carry markup.
    pre.textContent = output;
    problem.append(pre);
  }

  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.className = "btn btn--sm btn--quiet";
  dismiss.textContent = "Dismiss";
  dismiss.addEventListener("click", () => problem.remove());
  problem.append(dismiss);

  page.prepend(problem);
  problem.scrollIntoView({ block: "nearest" });
}

/**
 * Pull the readable parts out of whatever an endpoint answered with.
 *
 * @param {XMLHttpRequest} xhr The failed request.
 * @returns {{fix: string, output: string}} What to say, and what to show.
 */
function readError(xhr) {
  const body = xhr.responseText || "";
  try {
    const payload = JSON.parse(body);
    const detail = payload.detail || payload.message || xhr.statusText;
    // The API's ErrorResponse carries the actionable half separately.
    return { fix: payload.hint || detail, output: payload.hint ? detail : payload.output || "" };
  } catch {
    return { fix: `The server refused that (${xhr.status} ${xhr.statusText})`, output: body };
  }
}

/* ------------------------------------------------------------ log drawer */

/**
 * The docked log drawer.
 *
 * It is created once, in the shell, and survives navigation, so a deploy
 * started on one screen keeps streaming while you look at another. That is the
 * whole reason it is docked rather than modal.
 *
 * **Why this is hand-written and not an Alpine component.** The panel serves
 * itself under `script-src 'self'` with no `unsafe-eval`, because it executes
 * systemd as root and an injected script is a root shell. Alpine evaluates
 * every `x-data`, `@click` and `x-text` by turning a string into a function,
 * which is exactly what that policy forbids: the browser blocked all of them,
 * the component never initialised, and every Logs button in the panel did
 * nothing. Loosening the policy to make a 47 KB dependency work, for two small
 * islands of behaviour, is the wrong trade in this product.
 */
const drawer = {
  element: null,
  source: null,
  terminal: null,
  socket: null,

  /** What the find field last asked for. */
  query: "",
  /** Buffer positions of the current find, as {row, column} pairs. */
  matches: [],
  /** Which match is highlighted, an index into matches. */
  matchIndex: -1,

  /** @returns {boolean} Whether the drawer is open. */
  get open() {
    return this.element?.dataset.open === "true";
  },

  /**
   * Find the drawer in the current document and wire its controls.
   */
  mount() {
    this.element = document.getElementById("log-drawer");
    if (!this.element) return;

    window.wasmPanel = window.wasmPanel || {};
    window.wasmPanel.followLog = (source, url) => this.follow(source, url);
    window.wasmPanel.drawer = this;

    this.setOpen(false);
    this.render();
  },

  /**
   * Open or close the drawer, keeping the notices stack clear of it.
   *
   * The offset is a custom property the stylesheet already reads; nothing ever
   * set it, so toasts painted on top of the open drawer.
   *
   * @param {boolean} open Whether the drawer should be open.
   */
  setOpen(open) {
    if (!this.element) return;
    this.element.dataset.open = open ? "true" : "false";
    document.documentElement.style.setProperty(
      "--log-drawer-offset",
      open ? DRAWER_HEIGHT : "0px",
    );
    this.render();
    this.updateJump();
  },

  /**
   * Bring the bar's text and the toggle's label back in step with the state.
   */
  render() {
    if (!this.element) return;

    const label = this.element.querySelector("[data-drawer-source]");
    if (label) label.textContent = this.source || "nothing attached";

    for (const button of this.element.querySelectorAll("[data-drawer-toggle]")) {
      if (button.tagName === "BUTTON") {
        button.textContent = this.open ? "Hide" : "Show";
        button.setAttribute("aria-expanded", this.open ? "true" : "false");
      }
    }
  },

  toggle() {
    this.setOpen(!this.open);
    if (this.open) this.ensureTerminal();
  },

  /**
   * Create the terminal on first use.
   *
   * xterm is 480 KB, so it is only constructed when the drawer is actually
   * opened rather than on every page load.
   */
  async ensureTerminal() {
    if (this.terminal) {
      this.terminal.focus();
      return;
    }
    if (!window.Terminal) {
      await import("/static/vendor/xterm.js");
    }
    const styles = getComputedStyle(document.documentElement);
    this.terminal = new window.Terminal({
      fontFamily: styles.getPropertyValue("--font-mono").trim(),
      fontSize: 12,
      convertEol: true,
      scrollback: 5000,
      theme: {
        // The terminal sits on the drawer's sunken ground, one step below
        // the bar, so the log reads as a well set into the page.
        background: styles.getPropertyValue("--bg-sunken").trim(),
        foreground: styles.getPropertyValue("--text").trim(),
      },
    });
    this.terminal.open(document.getElementById("log-drawer-body"));
    // xterm stops following the tail on its own the moment the viewport is
    // scrolled up; this keeps the way back on screen exactly while that
    // pause is in effect.
    this.terminal.onScroll(() => this.updateJump());
  },

  /**
   * Show or hide the way back to the tail.
   *
   * The pause itself is xterm's: output written while the viewport is
   * scrolled up does not drag it back down. What was missing is any sign
   * that following has stopped, so the button appears whenever the viewport
   * is above the bottom and names the way back.
   */
  updateJump() {
    const jump = document.querySelector("[data-drawer-jump]");
    if (!jump) return;
    if (!this.terminal || !this.open) {
      jump.hidden = true;
      return;
    }
    const buffer = this.terminal.buffer.active;
    jump.hidden = buffer.viewportY >= buffer.baseY;
  },

  /** Return to the tail and resume following it. */
  jumpToBottom() {
    if (this.terminal) this.terminal.scrollToBottom();
    this.updateJump();
  },

  /**
   * Find every occurrence of a query in the terminal's buffer.
   *
   * The vendored xterm build ships without the search addon, and the buffer
   * API already exposes every line while selection is already a highlight
   * the terminal knows how to draw, so this stays first-party rather than
   * vendoring more third-party script into a root panel. Case-insensitive,
   * because an operator hunting an error does not know how it was cased.
   *
   * @param {string} query What to look for. Empty clears the find.
   */
  search(query) {
    this.query = query;
    this.matches = [];
    this.matchIndex = -1;

    if (this.terminal && query) {
      const needle = query.toLowerCase();
      const buffer = this.terminal.buffer.active;
      for (let row = 0; row < buffer.length; row += 1) {
        const line = buffer.getLine(row);
        if (!line) continue;
        const text = line.translateToString(true).toLowerCase();
        for (let at = text.indexOf(needle); at !== -1; at = text.indexOf(needle, at + 1)) {
          this.matches.push({ row, column: at });
        }
      }
    }

    if (this.matches.length) {
      this.showMatch(0);
    } else {
      if (this.terminal) this.terminal.clearSelection();
      this.renderCount();
    }
  },

  /**
   * Step to another match.
   *
   * The buffer keeps growing under a live stream, so an empty result is
   * retried against the current content before giving up: the line being
   * hunted may have arrived since the query was typed.
   *
   * @param {number} step How far to move through the matches, +1 or -1.
   */
  findNext(step) {
    if (!this.query) return;
    if (!this.matches.length) {
      this.search(this.query);
      return;
    }
    this.showMatch(this.matchIndex + step);
  },

  /**
   * Highlight one match and bring it on screen.
   *
   * @param {number} index Which match; wraps around either end.
   */
  showMatch(index) {
    const count = this.matches.length;
    this.matchIndex = ((index % count) + count) % count;
    const match = this.matches[this.matchIndex];
    this.terminal.select(match.column, match.row, this.query.length);
    this.terminal.scrollToLine(match.row);
    this.updateJump();
    this.renderCount();
  },

  /** Keep the "3/17" figure beside the find field in step with it. */
  renderCount() {
    const count = document.querySelector("[data-drawer-count]");
    if (!count) return;
    count.hidden = !this.query;
    if (!this.query) {
      count.textContent = "";
    } else if (this.matches.length) {
      count.textContent = `${this.matchIndex + 1}/${this.matches.length}`;
    } else {
      count.textContent = "0 matches";
    }
  },

  /**
   * Write one frame from the log socket.
   *
   * Every frame is a JSON envelope, so writing event.data put
   * {"type":"log","data":"..."} on screen once per line. A frame that is not
   * JSON is still written rather than swallowed: an unparseable log line is
   * exactly the kind of thing an operator needs to see.
   *
   * @param {string} raw The frame as it arrived.
   */
  writeFrame(raw) {
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      this.terminal.writeln(raw);
      return;
    }

    switch (payload.type) {
      case "log":
      case "warning":
        this.terminal.writeln(payload.data ?? "");
        break;
      case "error":
        this.terminal.writeln(`[${payload.message}]`);
        break;
      case "connected":
      case "heartbeat":
      case "pong":
        break;
      default:
        this.terminal.writeln(payload.data ?? payload.message ?? raw);
    }
    // New lines move the bottom away from a paused viewport without firing a
    // scroll, so the way back is refreshed on every frame as well.
    this.updateJump();
  },

  /**
   * Attach the drawer to a log stream.
   *
   * @param {string} source Human-readable name of what is being followed.
   * @param {string} url WebSocket URL to stream from.
   */
  async follow(source, url) {
    this.setOpen(true);
    await this.ensureTerminal();

    if (this.socket) this.socket.close();

    this.source = source;
    this.render();
    this.terminal.clear();

    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const absolute = url.startsWith("ws") ? url : `${scheme}//${location.host}${url}`;

    this.socket = new WebSocket(absolute);
    this.socket.addEventListener("message", (event) => {
      this.writeFrame(event.data);
    });
    this.socket.addEventListener("close", (event) => {
      // 1000 is a clean close; anything else is worth telling the operator
      // about, because a log that silently stops looks like a quiet system.
      if (event.code !== 1000) {
        this.terminal.writeln(`\r\n[log stream closed: ${event.code} ${event.reason}]`);
        notify(`Log stream for ${source} closed`, "failed");
      }
      this.source = null;
      this.render();
    });
  },

  clear() {
    if (this.terminal) this.terminal.clear();
  },
};

/* ------------------------------------------------------- mobile navigation */

/**
 * The off-canvas navigation.
 *
 * The stylesheet has had a complete off-canvas panel for a long time and
 * nothing ever set the state that reveals it, so on a phone the navigation and
 * the sign-out button could not be reached at all.
 *
 * @param {boolean} open Whether the navigation should be on screen.
 */
function setNavOpen(open) {
  const sidebar = document.getElementById("sidebar");
  const toggle = document.querySelector("[data-nav-toggle]");
  const scrim = document.querySelector(".scrim");
  if (!sidebar) return;

  sidebar.dataset.open = open ? "true" : "false";
  toggle?.setAttribute("aria-expanded", open ? "true" : "false");
  if (scrim) scrim.hidden = !open;
}

/* ------------------------------------------------------------ live events */

/**
 * The newest metrics snapshot the stream delivered, for anything that draws:
 * metric name to value, exactly as the collector published it. The charts
 * read it from here rather than opening a stream of their own.
 */
window.__wasmMetrics = null;

/**
 * Refresh every [data-metric] figure on screen from a snapshot.
 *
 * Only elements that opted in are touched, so the machine strip's own swap -
 * which replaces the whole fragment - stays the source of truth for
 * everything else in it.
 *
 * @param {Object<string, number>} snapshot Metric name to value.
 */
function updateMetricNumbers(snapshot) {
  for (const element of document.querySelectorAll("[data-metric]")) {
    const value = snapshot[element.dataset.metric];
    if (typeof value === "number") element.textContent = value.toFixed(2);
  }
}

/**
 * The EventSource whose listeners are already attached, so a reconnect wires
 * the replacement exactly once.
 */
let wiredEventSource = null;

/**
 * Attach the panel's listeners to the shared /events stream.
 *
 * The connection itself belongs to the htmx SSE extension: sse-connect on the
 * body opens one EventSource per tab, and the machine strip's sse-swap rides
 * it. The extension keeps the object internal, but it announces every
 * connection - including its own reconnects - by firing htmx:sseOpen with the
 * source in the event detail, and that is how this script listens on the same
 * connection instead of holding a second one open per tab.
 *
 * A state change is the most important thing this panel can show, so a row
 * whose state changed gets a brief pulse on its rail. Everything else on the
 * screen stays still.
 *
 * @param {EventSource} source The stream the extension just opened.
 */
function wireEventSource(source) {
  if (!source || source === wiredEventSource) return;
  wiredEventSource = source;

  source.addEventListener("state", (message) => {
    const change = JSON.parse(message.data);

    // Tracked before the row lookup: a job's state matters to the favicon
    // even when nothing on the current page renders a row for it.
    if (change.state === "busy") {
      busyJobs.add(change.id);
    } else {
      busyJobs.delete(change.id);
    }
    updateFavicon();

    const row = document.getElementById(`row-${CSS.escape(change.id)}`);
    if (!row) return;

    row.classList.remove("row--active", "row--failed", "row--busy", "row--idle");
    row.classList.add(`row--${STATE_CLASS[change.state] || "idle"}`, "row--changed");
    window.setTimeout(() => row.classList.remove("row--changed"), 700);
  });

  source.addEventListener("notice", (message) => {
    const notice = JSON.parse(message.data);
    notify(notice.text, notice.state);
  });

  source.addEventListener("metrics", (message) => {
    let snapshot;
    try {
      snapshot = JSON.parse(message.data);
    } catch {
      return;
    }
    window.__wasmMetrics = snapshot;
    updateMetricNumbers(snapshot);
    appendLivePoint(snapshot);
    // The strip fragment may have swapped since the last look, and its
    // badges are the favicon's authority on failed units; this tick is the
    // cheapest clock that notices.
    updateFavicon();
  });
}

function connectEvents() {
  document.body.addEventListener("htmx:sseOpen", (event) => {
    wireEventSource(event.detail.source);
  });

  document.body.addEventListener("htmx:sseError", () => {
    // The extension reconnects on its own; saying so beats a silent gap.
    notify("Lost the live connection. Reconnecting.", "busy", 3000);
  });
}

if (document.getElementById("machine-strip")) {
  connectEvents();
}

/* ----------------------------------------------------------------- charts */

/**
 * The dashboard's chart band.
 *
 * Each `[data-chart]` container names a spec below. History is loaded from
 * `/api/metrics/{metric}` once, downsampled to the point budget, and the live
 * edge rides the same `metrics` snapshot the strip's figures use: one push per
 * tick, shifted out at the other end, `uplot.setData` in between. uPlot draws
 * to a canvas and animates nothing, which is exactly what
 * prefers-reduced-motion asks of a chart.
 */

/**
 * Most points a chart holds. History is averaged down to this budget on load
 * and the live edge shifts against it, so a repaint stays the same price
 * whatever the window: 120 points is one every 30 seconds over an hour and one
 * every 12 minutes over a day, both denser than the pixels they land on.
 */
const CHART_POINTS = 120;

/**
 * What each chart container draws.
 *
 * Keyed by the container's data-chart value. A series names the collector
 * metrics it reads and, when it is not a plain copy, how to combine them;
 * `ceiling` names a metric whose latest value caps the y axis, which is how
 * the memory chart keeps the machine's total in frame. The traces are the
 * accent: CPU, memory, disk and traffic are neutral metrics, not states, so
 * the state tones stay off these lines and appear only where they mean state
 * (the deploy marks). The network chart's second direction is the muted text
 * tone, so the pair stays readable without inventing a new hue.
 */
const CHART_SPECS = {
  "cpu.percent": {
    kind: "percent",
    series: [{ label: "cpu", tone: "--accent", metrics: ["cpu.percent"] }],
  },
  "mem.used_bytes": {
    kind: "bytes",
    ceiling: "mem.total_bytes",
    series: [{ label: "used", tone: "--accent", metrics: ["mem.used_bytes"] }],
  },
  "net.bytes_s": {
    kind: "rate",
    series: [
      { label: "rx", tone: "--accent", metrics: ["net.rx_bytes_s"] },
      { label: "tx", tone: "--text-muted", metrics: ["net.tx_bytes_s"] },
    ],
  },
  "disk.percent": {
    kind: "percent",
    series: [
      {
        label: "disk",
        tone: "--accent",
        metrics: ["disk.used_bytes", "disk.total_bytes"],
        combine: (used, total) => (total > 0 ? (used / total) * 100 : null),
      },
    ],
  },
};

/**
 * Per-application chart shapes, resolved by metric name.
 *
 * The dashboard's containers are enumerable, so CHART_SPECS keys them
 * outright; an application page's containers name the collector's per-app
 * metrics - app.{domain}.cpu.percent and app.{domain}.mem.bytes - and the
 * domain cannot be known here. Each shape recognises one family and builds
 * the same spec object the static table holds. The tones repeat the
 * dashboard's vocabulary: the accent for neutral metrics.
 */
const APP_CHART_SHAPES = [
  {
    pattern: /^app\..+\.cpu\.percent$/,
    build: (metric) => ({
      kind: "percent",
      series: [{ label: "cpu", tone: "--accent", metrics: [metric] }],
    }),
  },
  {
    pattern: /^app\..+\.mem\.bytes$/,
    build: (metric) => ({
      kind: "bytes",
      series: [{ label: "mem", tone: "--accent", metrics: [metric] }],
    }),
  },
];

/**
 * Resolve the spec a container's data-chart value names.
 *
 * @param {string} name The container's data-chart value.
 * @returns {Object|null} The spec, or null when nothing recognises the name.
 */
function chartSpec(name) {
  if (CHART_SPECS[name]) return CHART_SPECS[name];
  const shape = APP_CHART_SHAPES.find((candidate) => candidate.pattern.test(name));
  return shape ? shape.build(name) : null;
}

/**
 * Read the deploy marks a chart container carries.
 *
 * The application fragment embeds the domain's deployment history as a JSON
 * attribute - server-rendered, like the catalogue the palette reads - so the
 * client neither fetches nor invents it. Anything that does not parse into a
 * timestamped mark is dropped rather than guessed at.
 *
 * @param {Element} host The chart container.
 * @returns {Array<{ts: number, state: string}>} The marks, possibly empty.
 */
function readChartMarks(host) {
  const raw = host.getAttribute("data-chart-marks");
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.filter((mark) => typeof mark.ts === "number" && typeof mark.state === "string")
      : [];
  } catch {
    return [];
  }
}

/**
 * Paint one chart's deploy marks: a vertical dashed line per deployment, in
 * the outcome's state tone, so a step in the trace can be read against the
 * deploy that caused it.
 *
 * Drawn from uPlot's draw hook, clipped to the plot area, so a mark scrolls
 * with the data and leaves with its window. Colour only ever encodes state,
 * and a deploy's outcome is state: green landed, red failed.
 *
 * @param {Object} u The uPlot instance mid-draw.
 * @param {Object} chart The chart record carrying the marks.
 * @param {Object} theme What chartStyles read when the plot was built.
 */
function drawDeployMarks(u, chart, theme) {
  if (!chart.marks.length) return;
  const { min, max } = u.scales.x;
  if (min == null || max == null) return;

  const ctx = u.ctx;
  ctx.save();
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  for (const mark of chart.marks) {
    if (mark.ts < min || mark.ts > max) continue;
    const x = Math.round(u.valToPos(mark.ts, "x", true));
    ctx.strokeStyle = theme.tone(mark.state === "failed" ? "--state-failed" : "--state-active");
    ctx.beginPath();
    ctx.moveTo(x, u.bbox.top);
    ctx.lineTo(x, u.bbox.top + u.bbox.height);
    ctx.stroke();
  }
  ctx.restore();
}

/** The charts on screen: {host, spec, marks, plot, data, ceiling, load}. */
const liveCharts = [];

/** The window the band is showing. The buttons write it, buildChart reads it. */
let chartWindow = "1h";

/** The in-flight load of uPlot, so two charts do not inject it twice. */
let uplotLoading = null;

/**
 * Load uPlot and its stylesheet, once, only when a chart is on screen.
 *
 * The vendored build is an IIFE whose top-level `var uPlot` only becomes a
 * window property under classic-script semantics; `import()` would evaluate
 * it in module scope and keep the global for itself, which is why this is a
 * script element rather than the dynamic import xterm gets away with (xterm's
 * UMD wrapper assigns to globalThis explicitly). Both files are same-origin,
 * so script-src 'self' and style-src 'self' hold.
 *
 * @returns {Promise<void>} Resolves when window.uPlot exists.
 */
function ensureUplot() {
  if (window.uPlot) return Promise.resolve();
  if (!uplotLoading) {
    const style = document.createElement("link");
    style.rel = "stylesheet";
    style.href = "/static/vendor/uplot.min.css";
    document.head.append(style);

    uplotLoading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/static/vendor/uplot.min.js";
      script.addEventListener("load", () => resolve());
      script.addEventListener("error", () => reject(new Error("uPlot failed to load")));
      document.head.append(script);
    });
  }
  return uplotLoading;
}

/**
 * Render a byte count the way an operator reads one.
 *
 * @param {number|null} value Bytes.
 * @returns {string} A short figure, e.g. "1.5 GB".
 */
function formatBytes(value) {
  if (typeof value !== "number" || !isFinite(value)) return "";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let unit = 0;
  let magnitude = Math.abs(value);
  while (magnitude >= 1024 && unit < units.length - 1) {
    magnitude /= 1024;
    unit += 1;
  }
  const scaled = value / 1024 ** unit;
  return `${scaled.toFixed(magnitude >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

/**
 * Turn a hex colour into the same colour at low opacity, for area fills.
 *
 * @param {string} colour A "#rrggbb" string, as the theme tokens are written.
 * @param {number} alpha Opacity between 0 and 1.
 * @returns {string|undefined} An rgba() string, or undefined when the token
 *   is not the hex this expects, in which case the series simply has no fill.
 */
function withAlpha(colour, alpha) {
  const match = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(colour);
  if (!match) return undefined;
  const [red, green, blue] = [match[1], match[2], match[3]].map((pair) => parseInt(pair, 16));
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

/**
 * Read what the charts take from the design system, at this moment.
 *
 * Read at draw time rather than held, because the tokens change with the
 * theme and a chart drawn from stale values is a chart in the wrong mode.
 *
 * @returns {{axis: string, grid: string, font: string, tone: function}} What
 *   uPlot needs: axis text colour, grid colour, the label font, and a reader
 *   for the state tones the series are stroked with.
 */
function chartStyles() {
  const styles = getComputedStyle(document.documentElement);
  const read = (name) => styles.getPropertyValue(name).trim();
  return {
    axis: read("--text-muted"),
    grid: read("--border"),
    // Axis figures are system values, so they speak mono like every other one.
    font: `400 10px ${read("--font-mono") || "monospace"}`,
    tone: read,
  };
}

/**
 * Compute one drawn value for a series from a metric reader.
 *
 * @param {Object} series A series out of CHART_SPECS.
 * @param {function(string): (number|undefined)} read Metric name to value.
 * @returns {number|null} The value to plot, or null when any input is absent.
 */
function seriesValue(series, read) {
  const inputs = series.metrics.map((metric) => {
    const value = read(metric);
    return typeof value === "number" ? value : null;
  });
  if (inputs.some((value) => value === null)) return null;
  return series.combine ? series.combine(...inputs) : inputs[0];
}

/**
 * Fetch one metric's history.
 *
 * @param {string} metric Metric name.
 * @returns {Promise<Array<[number, number]>>} Oldest-first [ts, value] pairs.
 *   Empty on any failure: "no data yet" is a normal chart state, the live
 *   edge still arrives over the stream, and a toast per chart per visit would
 *   drown the notices that matter.
 */
async function metricHistory(metric) {
  try {
    const response = await fetch(
      `/api/metrics/${encodeURIComponent(metric)}?window=${chartWindow}`,
    );
    if (!response.ok) return [];
    const payload = await response.json();
    return Array.isArray(payload.points) ? payload.points : [];
  } catch {
    return [];
  }
}

/**
 * Average rows down to the point budget.
 *
 * @param {Array<Array<number|null>>} rows [ts, v1, ...] rows, oldest first.
 * @param {number} cap Most rows to return.
 * @returns {Array<Array<number|null>>} At most cap rows, each the mean of its
 *   bucket, stamped with the bucket's newest time.
 */
function downsample(rows, cap) {
  if (rows.length <= cap) return rows;
  const size = Math.ceil(rows.length / cap);
  const out = [];
  for (let start = 0; start < rows.length; start += size) {
    const bucket = rows.slice(start, start + size);
    out.push(
      bucket[bucket.length - 1].map((_, column) => {
        if (column === 0) return bucket[bucket.length - 1][0];
        const values = bucket.map((row) => row[column]).filter((v) => typeof v === "number");
        if (!values.length) return null;
        return values.reduce((total, v) => total + v, 0) / values.length;
      }),
    );
  }
  return out;
}

/**
 * Assemble uPlot data from per-metric histories.
 *
 * Series are merged on their timestamps rather than by index: the collector
 * writes every metric of a tick with one stamp, but consolidation and gaps
 * make counting unreliable, and a merge that guesses draws lies.
 *
 * @param {Object} spec The chart's spec.
 * @param {Object<string, Array<[number, number]>>} histories Metric to points.
 * @returns {Array<Array<number|null>>} uPlot column data: [xs, s1, ...].
 */
function toChartData(spec, histories) {
  const stamps = new Set();
  const byMetric = new Map();
  for (const metric of new Set(spec.series.flatMap((series) => series.metrics))) {
    const values = new Map();
    for (const [ts, value] of histories[metric] || []) {
      values.set(ts, value);
      stamps.add(ts);
    }
    byMetric.set(metric, values);
  }

  const rows = [...stamps]
    .sort((first, second) => first - second)
    .map((ts) => [
      ts,
      ...spec.series.map((series) => seriesValue(series, (metric) => byMetric.get(metric).get(ts))),
    ]);

  const data = [[], ...spec.series.map(() => [])];
  for (const row of downsample(rows, CHART_POINTS)) {
    row.forEach((value, column) => data[column].push(value ?? null));
  }
  return data;
}

/**
 * Build uPlot options for one chart, in the current theme.
 *
 * @param {Object} chart The chart record.
 * @param {Object} theme What chartStyles read.
 * @returns {Object} uPlot options.
 */
function chartOptions(chart, theme) {
  const spec = chart.spec;
  const gridLine = { stroke: theme.grid, width: 1 };

  const values =
    spec.kind === "percent"
      ? (u, ticks) => ticks.map((v) => (v == null ? "" : `${v}%`))
      : (u, ticks) => ticks.map((v) => (v == null ? "" : formatBytes(v) + (spec.kind === "rate" ? "/s" : "")));
  const range =
    spec.kind === "percent"
      ? [0, 100]
      : (u, min, max) => [0, chart.ceiling || (max > 0 ? max * 1.15 : 1)];

  return {
    width: Math.max(chart.host.clientWidth, 40),
    height: Math.max(chart.host.clientHeight, 40),
    // No legend and no cursor: the band is an instrument trace, the caption
    // names it, and half an affordance (a crosshair with nowhere to print its
    // reading) is worse than none.
    legend: { show: false },
    cursor: { show: false },
    hooks: {
      draw: [(u) => drawDeployMarks(u, chart, theme)],
    },
    scales: {
      x: {
        time: true,
        // A store that started seconds ago hands a chart one point, and
        // uPlot pads that degenerate span into years of empty axis. A
        // minute is the narrowest honest window while the band fills.
        range: (u, min, max) =>
          min == null || max == null || max - min >= 60 ? [min, max] : [max - 60, max],
      },
      y: { range },
    },
    axes: [
      { stroke: theme.axis, font: theme.font, ticks: { ...gridLine }, grid: { show: false } },
      {
        stroke: theme.axis,
        font: theme.font,
        // Wide enough for the longest figure an axis prints ("977 KB/s");
        // one pixel short and the leading digit is clipped.
        size: 58,
        ticks: { ...gridLine },
        grid: { ...gridLine },
        values,
      },
    ],
    series: [
      {},
      ...spec.series.map((series) => {
        const stroke = theme.tone(series.tone);
        return {
          label: series.label,
          stroke,
          width: 1.5,
          // A soft fill under a single line reads as area without shouting;
          // two filled series would just occlude each other.
          fill: spec.series.length === 1 ? withAlpha(stroke, 0.08) : undefined,
          points: { show: false },
        };
      }),
    ],
  };
}

/**
 * Create (or re-create) the uPlot instance for a chart from its held data.
 *
 * @param {Object} chart The chart record, with data already assembled.
 */
function drawChart(chart) {
  if (chart.plot) chart.plot.destroy();
  chart.host.replaceChildren();
  chart.plot = new window.uPlot(chartOptions(chart, chartStyles()), chart.data, chart.host);
}

/**
 * Load a chart's history and draw it.
 *
 * @param {Object} chart The chart record.
 */
async function buildChart(chart) {
  const token = Symbol("load");
  chart.load = token;

  const metrics = [
    ...new Set(
      chart.spec.series
        .flatMap((series) => series.metrics)
        .concat(chart.spec.ceiling ? [chart.spec.ceiling] : []),
    ),
  ];
  const results = await Promise.all(metrics.map((metric) => metricHistory(metric)));

  // A stale response must not repaint over a newer window, and a container
  // that left with a navigation must not be drawn into.
  if (chart.load !== token || !chart.host.isConnected) return;

  const histories = {};
  metrics.forEach((metric, index) => {
    histories[metric] = results[index];
  });

  if (chart.spec.ceiling) {
    const points = histories[chart.spec.ceiling];
    if (points.length) chart.ceiling = points[points.length - 1][1];
  }

  chart.data = toChartData(chart.spec, histories);
  drawChart(chart);
}

/** Keeps every chart sized to the box the stylesheet gives it. */
const chartResizer =
  "ResizeObserver" in window
    ? new ResizeObserver((entries) => {
        for (const entry of entries) {
          const chart = liveCharts.find((candidate) => candidate.host === entry.target);
          if (chart && chart.plot) {
            chart.plot.setSize({
              width: Math.max(entry.contentRect.width, 40),
              height: Math.max(entry.contentRect.height, 40),
            });
          }
        }
      })
    : null;

/**
 * Find chart containers on the current page and bring them to life.
 *
 * Runs at load and after every htmx swap, like the QR sweep: hx-boost
 * replaces the page contents without re-running this script, so arriving on
 * the dashboard by navigation must find the containers too. Charts whose
 * elements a swap removed are destroyed here rather than leaked.
 */
async function initCharts() {
  for (let index = liveCharts.length - 1; index >= 0; index -= 1) {
    const chart = liveCharts[index];
    if (!chart.host.isConnected) {
      if (chart.plot) chart.plot.destroy();
      if (chartResizer) chartResizer.unobserve(chart.host);
      liveCharts.splice(index, 1);
    }
  }

  const hosts = [...document.querySelectorAll("[data-chart]")].filter(
    (host) => chartSpec(host.dataset.chart) && !liveCharts.some((chart) => chart.host === host),
  );
  if (!hosts.length) return;

  try {
    await ensureUplot();
  } catch {
    // The browser already reported the failed request on its own; an empty
    // band and a working panel beat a modal about a chart library.
    return;
  }

  for (const host of hosts) {
    const chart = {
      host,
      spec: chartSpec(host.dataset.chart),
      marks: readChartMarks(host),
      plot: null,
      data: null,
      ceiling: null,
      load: null,
    };
    liveCharts.push(chart);
    if (chartResizer) chartResizer.observe(host);
    buildChart(chart);
  }
}

/**
 * Append the newest snapshot to every live chart.
 *
 * @param {Object<string, number>} snapshot Metric name to value.
 */
function appendLivePoint(snapshot) {
  const now = Date.now() / 1000;
  for (const chart of liveCharts) {
    if (!chart.plot || !chart.data) continue;

    if (chart.spec.ceiling && typeof snapshot[chart.spec.ceiling] === "number") {
      chart.ceiling = snapshot[chart.spec.ceiling];
    }

    const values = chart.spec.series.map((series) =>
      seriesValue(series, (metric) => snapshot[metric]),
    );
    // The collector's first tick has no rates yet; a row of nulls would put a
    // gap on every chart for nothing.
    if (values.every((value) => value === null)) continue;

    chart.data[0].push(now);
    values.forEach((value, index) => chart.data[index + 1].push(value));
    while (chart.data[0].length > CHART_POINTS) {
      for (const column of chart.data) column.shift();
    }
    chart.plot.setData(chart.data);
  }
}

/**
 * Switch the band to another window and reload every chart's history.
 *
 * @param {string} next "1h" or "24h", from the button's data attribute.
 */
function setChartWindow(next) {
  if (next === chartWindow || !(next === "1h" || next === "24h")) return;
  chartWindow = next;
  for (const button of document.querySelectorAll("[data-chart-window]")) {
    button.setAttribute("aria-pressed", button.dataset.chartWindow === next ? "true" : "false");
  }
  for (const chart of liveCharts) buildChart(chart);
}

/**
 * Redraw every chart with its held data, re-reading the theme tokens.
 *
 * Called when the theme changes, by the toggle or by the operating system: a
 * canvas keeps the colours it was painted with, so unlike the rest of the
 * page it does not follow the custom properties on its own.
 */
function restyleCharts() {
  for (const chart of liveCharts) {
    if (chart.plot && chart.data) drawChart(chart);
  }
}

/* ---------------------------------------------------------------- favicon */

/**
 * The status favicon: the tab tells the machine's worst active state before
 * the tab is even opened, the way Vercel's deployment tabs do.
 *
 * The chosen semantics, from worst to best:
 *
 * - **failed**: the machine strip is showing failed units. The strip is
 *   server-rendered truth - systemd's own tally, re-pushed every five seconds
 *   over the stream - so it is present at first paint, it covers failures
 *   this tab never saw (a unit that died while nobody was looking, or was
 *   broken from an SSH session), and it clears itself when the unit is fixed.
 *   A failed *job* is not tracked separately here: one that breaks a unit
 *   shows up in the strip within a tick, and one that does not leaves a
 *   persistent problem notice, whereas a red tab no event will ever clear is
 *   a stuck alarm.
 * - **busy**: a job is running (state events with state "busy", cleared by
 *   the same id reaching any settled state), or the strip shows units
 *   activating.
 * - **ok**: everything else.
 */
const FAVICON_TONES = { failed: "--state-failed", busy: "--state-busy", ok: "--state-active" };

/** Ids the stream has reported busy and not yet reported settled. */
const busyJobs = new Set();

/** What the icon currently shows, as "state:colour", to skip no-op swaps. */
let faviconShown = null;

/**
 * Derive the worst active state from what this tab can see.
 *
 * @returns {"failed"|"busy"|"ok"} The state the icon should show.
 */
function faviconState() {
  const strip = document.getElementById("machine-strip");
  if (strip && strip.querySelector(".badge--failed")) return "failed";
  if (busyJobs.size || (strip && strip.querySelector(".badge--busy"))) return "busy";
  return "ok";
}

/**
 * Point the icon link at a dot of the right state tone.
 *
 * The dot is a static SVG data: URI built from the current theme's token, so
 * it needs no request and no asset; the panel's CSP allows it because img-src
 * already carries `data:`. Colour only ever encodes state, and this is state.
 */
function updateFavicon() {
  const link = document.getElementById("favicon");
  if (!link) return;

  const state = faviconState();
  const colour =
    getComputedStyle(document.documentElement).getPropertyValue(FAVICON_TONES[state]).trim() ||
    "#6b7178";
  const shown = `${state}:${colour}`;
  if (shown === faviconShown) return;
  faviconShown = shown;

  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">` +
    `<circle cx="8" cy="8" r="6" fill="${colour}"/></svg>`;
  link.href = `data:image/svg+xml,${encodeURIComponent(svg)}`;
  link.type = "image/svg+xml";
}

/** Force the next update to redraw, because the theme's tones changed. */
function refreshFaviconColours() {
  faviconShown = null;
  updateFavicon();
}

/* ------------------------------------------------------------ 2FA QR code */

/**
 * Draw the two-factor enrolment QR into every empty [data-totp-qr] host.
 *
 * The server sends the otpauth:// URI as a data attribute and the QR is
 * encoded here, by the vendored qrcodegen (Nayuki, MIT), so the secret never
 * travels to a third party the way a QR image service would require. The
 * module is ~45 KB and enrolment is rare, so it is imported only when an
 * enrolment is actually on screen.
 *
 * The code is black on white whatever the theme: this square is read by a
 * phone camera, not by the operator, and scanners want maximum contrast. The
 * base32 key next to it is the fallback for a browser where this never runs.
 */
async function renderTotpQrs() {
  for (const host of document.querySelectorAll("[data-totp-qr]")) {
    if (host.querySelector("canvas")) continue;
    const uri = host.dataset.totpUri;
    if (!uri) continue;

    const { default: qrcodegen } = await import("/static/vendor/qrcodegen.js");
    const qr = qrcodegen.QrCode.encodeText(uri, qrcodegen.QrCode.Ecc.MEDIUM);

    const border = 2;
    const scale = 4;
    const size = (qr.size + border * 2) * scale;
    const canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", "Enrolment QR code. The key below encodes the same secret.");

    const context = canvas.getContext("2d");
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, size, size);
    context.fillStyle = "#000000";
    for (let y = 0; y < qr.size; y += 1) {
      for (let x = 0; x < qr.size; x += 1) {
        if (qr.getModule(x, y)) {
          context.fillRect((x + border) * scale, (y + border) * scale, scale, scale);
        }
      }
    }
    host.replaceChildren(canvas);
  }
}

/* ------------------------------------------------------- command palette */

/**
 * The Ctrl+K palette: type a few letters, land on any screen or application.
 *
 * The catalogue is server-rendered into #palette-data by the shell - the
 * fixed screens plus every deployed application - so opening it costs no
 * request and shows exactly what the server last knew. This side only
 * filters. Everything is built with createElement and textContent: a domain
 * name goes through here, and this panel is a root shell.
 *
 * A native dialog, because showModal traps focus and closes on Escape
 * without either being hand-rolled.
 */
const palette = {
  /** The catalogue as read at open, so a swap mid-use cannot shift entries. */
  entries: [],
  /** The entries the current query keeps, in rank order. */
  filtered: [],
  /** Which entry is selected, an index into filtered. */
  index: 0,

  /** @returns {HTMLDialogElement|null} The dialog, fresh from the document. */
  dialog() {
    return document.getElementById("command-palette");
  },

  /**
   * Read the server-rendered catalogue.
   *
   * Re-read at every open rather than held: hx-boost replaces the body's
   * contents, and the block that arrives with a new page is the truth about
   * what exists now.
   *
   * @returns {Array<{label: string, href: string, hint: string}>} Entries.
   */
  catalogue() {
    const holder = document.getElementById("palette-data");
    if (!holder) return [];
    try {
      const parsed = JSON.parse(holder.textContent);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  },

  /** Open the palette empty, or close it if it is already up. */
  toggle() {
    const dialog = this.dialog();
    if (!dialog) return;
    if (dialog.open) {
      dialog.close();
      return;
    }

    this.entries = this.catalogue();
    const input = document.getElementById("palette-input");
    if (input) input.value = "";
    dialog.showModal();
    this.filter("");
    if (input) input.focus();
  },

  /**
   * Keep the entries the query matches, best first.
   *
   * Subsequence matching, so "dbs" finds Databases and a few letters of a
   * domain find the application; a prefix outranks a substring outranks a
   * scattered subsequence, and rank ties keep the catalogue's own order.
   *
   * @param {string} query What the operator has typed.
   */
  filter(query) {
    const needle = query.trim().toLowerCase();
    const ranked = [];
    for (const entry of this.entries) {
      const rank = Math.min(matchRank(needle, entry.label), matchRank(needle, entry.href));
      if (rank !== Infinity) ranked.push([rank, entry]);
    }
    ranked.sort((first, second) => first[0] - second[0]);
    this.filtered = ranked.map((pair) => pair[1]);
    this.index = 0;
    this.render();
  },

  /** Rebuild the list to match filtered and the selection. */
  render() {
    const list = document.getElementById("palette-list");
    if (!list) return;
    list.replaceChildren();

    if (!this.filtered.length) {
      const empty = document.createElement("li");
      empty.className = "palette__empty";
      empty.textContent = "Nothing matches.";
      list.append(empty);
    }

    this.filtered.forEach((entry, index) => {
      const item = document.createElement("li");
      item.className = "palette__item";
      item.id = `palette-item-${index}`;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", index === this.index ? "true" : "false");

      const label = document.createElement("span");
      label.className = "palette__label";
      label.textContent = entry.label;

      const href = document.createElement("span");
      href.className = "palette__href";
      href.textContent = entry.href;

      item.append(label, href);
      list.append(item);
    });

    const input = document.getElementById("palette-input");
    if (input) {
      input.setAttribute(
        "aria-activedescendant",
        this.filtered.length ? `palette-item-${this.index}` : "",
      );
    }
  },

  /**
   * Move the selection.
   *
   * @param {number} step +1 or -1; wraps around either end.
   */
  move(step) {
    if (!this.filtered.length) return;
    const count = this.filtered.length;
    this.index = (this.index + step + count) % count;
    this.render();
    document.getElementById(`palette-item-${this.index}`)?.scrollIntoView({ block: "nearest" });
  },

  /**
   * Go to an entry.
   *
   * A plain navigation rather than an htmx swap: the palette is how the
   * operator leaves this screen, and a full load re-renders the catalogue
   * on arrival.
   *
   * @param {number} index Position in the filtered list.
   */
  choose(index) {
    const entry = this.filtered[index];
    if (!entry) return;
    this.dialog()?.close();
    window.location.assign(entry.href);
  },
};

/**
 * How well a query matches a candidate. Lower is better.
 *
 * @param {string} needle The query, lowercased.
 * @param {string} candidate The text to match against.
 * @returns {number} 0 for an empty query, 1 for a prefix, 2 for a substring,
 *   3 for a subsequence, Infinity for no match.
 */
function matchRank(needle, candidate) {
  if (!needle) return 0;
  const text = String(candidate || "").toLowerCase();
  if (text.startsWith(needle)) return 1;
  if (text.includes(needle)) return 2;

  let at = 0;
  for (const character of needle) {
    at = text.indexOf(character, at);
    if (at === -1) return Infinity;
    at += 1;
  }
  return 3;
}

renderTotpQrs();
initCharts();
updateFavicon();

// A theme change from the operating system repaints the page through the
// media query, but not the canvases or the icon.
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  restyleCharts();
  refreshFaviconColours();
});

// The enrolment fragment arrives by htmx swap; the whole document is swept
// rather than the swap target, because outerHTML swaps replace the target and
// which element the event lands on differs between swap styles. The chart
// sweep rides the same event for the same reason: hx-boost replaces the page
// contents without re-running this script.
document.body.addEventListener("htmx:afterSwap", () => {
  renderTotpQrs();
  initCharts();
});

/* --------------------------------------------------------------- wiring */

/*
 * One delegated click listener for the whole shell.
 *
 * Delegation rather than inline handlers, for two reasons. A domain name or a
 * unit name never travels inside an attribute value where it could break out
 * of it - and this panel is a root shell, so that matters more here than
 * almost anywhere. And an inline handler is a string the browser has to
 * evaluate, which the panel's own Content Security Policy forbids.
 */
document.body.addEventListener("click", (event) => {
  const target = event.target;

  const log = target.closest("[data-follow-log]");
  if (log) {
    event.preventDefault();
    drawer.follow(log.dataset.source, log.dataset.url);
    return;
  }

  if (target.closest("[data-drawer-clear]")) {
    event.stopPropagation();
    drawer.clear();
    return;
  }

  // The find field sits inside the clickable bar; a click that lands in it
  // must focus it, not slam the drawer shut around it.
  if (target.closest("[data-drawer-search]")) {
    return;
  }

  if (target.closest("[data-drawer-jump]")) {
    drawer.jumpToBottom();
    return;
  }

  if (target.closest("[data-drawer-toggle]")) {
    drawer.toggle();
    return;
  }

  const paletteItem = target.closest(".palette__item");
  if (paletteItem && paletteItem.parentElement) {
    palette.choose([...paletteItem.parentElement.children].indexOf(paletteItem));
    return;
  }

  // A click on the dialog element itself can only be the backdrop area: the
  // input, the list and the hint tile the interior completely.
  if (target.id === "command-palette") {
    palette.toggle();
    return;
  }

  if (target.closest("[data-nav-toggle]")) {
    setNavOpen(document.getElementById("sidebar")?.dataset.open !== "true");
    return;
  }

  if (target.closest("[data-nav-close]")) {
    setNavOpen(false);
    return;
  }

  const windowButton = target.closest("[data-chart-window]");
  if (windowButton) {
    setChartWindow(windowButton.dataset.chartWindow);
    return;
  }

  if (target.closest("[data-theme-toggle]")) {
    toggleTheme();
    // Canvases and the icon hold painted colours; everything else on the
    // page follows the custom properties by itself.
    restyleCharts();
    refreshFaviconColours();
  }
});

document.addEventListener("keydown", (event) => {
  // Ctrl+K / Cmd+K from anywhere, including inside the palette, where it
  // closes again. It has to win against the browser's own address-bar
  // shortcut, hence the preventDefault.
  if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k") {
    event.preventDefault();
    palette.toggle();
    return;
  }

  const target = event.target instanceof Element ? event.target : null;

  if (target && target.id === "palette-input") {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      palette.move(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      palette.move(-1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      palette.choose(palette.index);
    }
    // Escape falls through to the dialog's own cancel behaviour.
    return;
  }

  if (target && target.closest("[data-drawer-search]")) {
    if (event.key === "Enter") {
      event.preventDefault();
      drawer.findNext(event.shiftKey ? -1 : 1);
    } else if (event.key === "Escape") {
      target.value = "";
      drawer.search("");
      target.blur();
    }
    return;
  }

  if (event.key === "Escape") setNavOpen(false);
});

// Filtering rides input events so it works per keystroke, and it is
// delegated for the same reason every other listener here is: the elements
// it watches are replaced by swaps, the body is not.
document.body.addEventListener("input", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;

  if (target.closest("[data-drawer-search]")) {
    drawer.search(target.value);
    return;
  }

  if (target.id === "palette-input") {
    palette.filter(target.value);
  }
});

// The shell is one document; htmx swaps fragments inside it, so this runs once
// and the delegated listeners above cover everything that arrives later.
drawer.mount();

/**
 * Read a cookie the server left for the browser to send back.
 *
 * @param {string} name Cookie name.
 * @returns {string|null} Its value, or null when it is not set.
 */
function readCookie(name) {
  const prefix = `${name}=`;
  for (const part of document.cookie.split("; ")) {
    if (part.startsWith(prefix)) return decodeURIComponent(part.slice(prefix.length));
  }
  return null;
}

/*
 * The CSRF token, taken from the cookie at the moment of the request.
 *
 * The shell also carries the token it was rendered with, in hx-headers, and
 * that is the value used until this listener runs. It is only correct until
 * the session renews itself, which happens silently at half its lifetime and
 * rotates the CSRF token as it goes: from then on every control in a tab left
 * open answered 403, and nothing on screen suggested that reloading would fix
 * it. The cookie always holds the current value.
 *
 * The name of the header and the name of the cookie both come from the server,
 * on the body element, because a constant spelled in two files is a constant
 * that drifts - which is exactly how the shell once sent "X-CSRF-Token" to a
 * server reading "X-WASM-CSRF".
 */
document.body.addEventListener("htmx:configRequest", (event) => {
  const { csrfHeader, csrfCookie } = document.body.dataset;
  if (!csrfHeader || !csrfCookie) return;

  const token = readCookie(csrfCookie);
  if (token) event.detail.headers[csrfHeader] = token;
});

/*
 * Every mutating control in the panel is hx-swap="none", because the API
 * answers in JSON and swapping a payload into the page is how a delete used to
 * leave a blob of braces where a row had been. The consequence was that a
 * successful action changed nothing on screen at all: Restart, Stop, Renew and
 * Restore did their work and reported it nowhere. This is where they report.
 */
document.body.addEventListener("htmx:afterRequest", (event) => {
  const { successful, xhr, elt } = event.detail;
  if (!successful || xhr.status >= 400) return;
  if (elt.getAttribute("hx-swap") !== "none") return;

  let message = elt.dataset.success;
  if (!message) {
    try {
      message = JSON.parse(xhr.responseText).message;
    } catch {
      message = null;
    }
  }
  if (message) notify(message, "active");
});

document.body.addEventListener("htmx:responseError", (event) => {
  // The server sends the real message from nginx, systemd or certbot. It is
  // never paraphrased here.
  const { fix, output } = readError(event.detail.xhr);
  showProblem(fix, output);
});

document.body.addEventListener("htmx:sendError", () => {
  notify("Could not reach the server", "failed");
});

/*
 * A button whose hx-target does not exist never sends its request, and htmx
 * says so only on the console. The delete button on an application's own page
 * was aimed at "closest .row" on a page that has no rows, so it was inert with
 * no sign of it.
 */
document.body.addEventListener("htmx:targetError", (event) => {
  showProblem(
    "That control is misconfigured and sent nothing. This is a bug in the panel.",
    `hx-target="${event.detail.target}" matched no element.`,
  );
});
