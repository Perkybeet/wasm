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
        background: styles.getPropertyValue("--surface").trim(),
        foreground: styles.getPropertyValue("--text").trim(),
      },
    });
    this.terminal.open(document.getElementById("log-drawer-body"));
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
 * Subscribe to server-sent state changes.
 *
 * A state change is the most important thing this panel can show, so a row
 * whose state changed gets a brief pulse on its rail. Everything else on the
 * screen stays still.
 */
function connectEvents() {
  const events = new EventSource("/events");

  events.addEventListener("state", (message) => {
    const change = JSON.parse(message.data);
    const row = document.getElementById(`row-${CSS.escape(change.id)}`);
    if (!row) return;

    row.classList.remove("row--active", "row--failed", "row--busy", "row--idle");
    row.classList.add(`row--${STATE_CLASS[change.state] || "idle"}`, "row--changed");
    window.setTimeout(() => row.classList.remove("row--changed"), 700);
  });

  events.addEventListener("notice", (message) => {
    const notice = JSON.parse(message.data);
    notify(notice.text, notice.state);
  });

  events.addEventListener("error", () => {
    // EventSource reconnects on its own; saying so beats a silent gap.
    notify("Lost the live connection. Reconnecting.", "busy", 3000);
  });
}

if (document.getElementById("machine-strip")) {
  connectEvents();
}

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

  if (target.closest("[data-drawer-toggle]")) {
    drawer.toggle();
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

  if (target.closest("[data-theme-toggle]")) toggleTheme();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setNavOpen(false);
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
