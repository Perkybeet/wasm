/*
 * Panel behaviour that htmx cannot express.
 *
 * There is deliberately very little here. Navigation, forms and partial
 * updates are hypermedia; this file covers the three things that genuinely
 * need a client: a terminal for streaming logs, notices that outlive a swap,
 * and the theme toggle.
 *
 * No build step. This is an ES module served as written.
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

/* ------------------------------------------------------------------ theme */

const THEME_KEY = "wasm.theme";

/**
 * Apply a theme and remember it.
 *
 * @param {"light"|"dark"|null} theme Theme to apply, or null to follow the
 *   operating system.
 */
export function setTheme(theme) {
  if (theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  } else {
    delete document.documentElement.dataset.theme;
    localStorage.removeItem(THEME_KEY);
  }
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
export function notify(text, state = "idle", ttl = 5000) {
  const host = document.getElementById("notices");
  if (!host) return;

  const item = document.createElement("div");
  item.className = `notice__item notice__item--${STATE_CLASS[state] || "idle"}`;
  item.textContent = text;
  host.append(item);

  window.setTimeout(() => item.remove(), ttl);
}

/* ------------------------------------------------------------ log drawer */

/**
 * The docked log drawer.
 *
 * It is created once, in the shell, and survives navigation, so a deploy
 * started on one screen keeps streaming while you look at another. That is
 * the whole reason it is docked rather than modal.
 *
 * @returns {object} Alpine component state.
 */
function logDrawer() {
  return {
    open: false,
    source: null,
    terminal: null,
    socket: null,

    init() {
      window.wasmPanel = window.wasmPanel || {};
      window.wasmPanel.followLog = (source, url) => this.follow(source, url);
      window.wasmPanel.drawer = this;
    },

    toggle() {
      this.open = !this.open;
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
      this.terminal.open(this.$refs.terminal);
    },

    /**
     * Attach the drawer to a log stream.
     *
     * @param {string} source Human-readable name of what is being followed.
     * @param {string} url WebSocket URL to stream from.
     */
    async follow(source, url) {
      this.open = true;
      await this.ensureTerminal();

      if (this.socket) this.socket.close();

      this.source = source;
      this.terminal.clear();

      const scheme = location.protocol === "https:" ? "wss:" : "ws:";
      const absolute = url.startsWith("ws") ? url : `${scheme}//${location.host}${url}`;

      this.socket = new WebSocket(absolute);
      this.socket.addEventListener("message", (event) => {
        this.terminal.writeln(event.data);
      });
      this.socket.addEventListener("close", (event) => {
        // 1000 is a clean close; anything else is worth telling the operator
        // about, because a log that silently stops looks like a quiet system.
        if (event.code !== 1000) {
          this.terminal.writeln(`\r\n[log stream closed: ${event.code} ${event.reason}]`);
          notify(`Log stream for ${source} closed`, "failed");
        }
        this.source = null;
      });
    },

    clear() {
      if (this.terminal) this.terminal.clear();
    },
  };
}

window.logDrawer = logDrawer;

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

/* ----------------------------------------------------------- htmx wiring */

/*
 * Delegated listener for the log buttons.
 *
 * Delegation rather than an inline handler, so that a domain name or a unit
 * name never travels inside an attribute value where it could break out of it.
 * It also survives htmx swaps for free.
 */
document.body.addEventListener("click", (event) => {
  const button = event.target.closest("[data-follow-log]");
  if (!button) return;
  event.preventDefault();
  window.wasmPanel?.followLog(button.dataset.source, button.dataset.url);
});

document.body.addEventListener("htmx:responseError", (event) => {
  // The server sends the real message from nginx, systemd or certbot. It is
  // never paraphrased here.
  const detail = event.detail.xhr.responseText || event.detail.xhr.statusText;
  notify(detail.slice(0, 300), "failed", 10000);
});

document.body.addEventListener("htmx:sendError", () => {
  notify("Could not reach the server", "failed");
});
