/**
 * Global browser error capture — forwards JS errors, unhandled promise
 * rejections, Vue component errors, and deprecation warnings to the backend
 * /api/feedback/client-error endpoint.
 *
 * Rationale (Doug, 2026-04-12): "how is a feature supposed to work right if
 * there is console errors? why are those errors in the console not being
 * logged somewhere?" useApi.js logged API 4xx/5xx but not general JS errors.
 * This closes that gap.
 *
 * Dedupes identical messages in a 5-second window to prevent noise storms.
 */

const RECENT_WINDOW_MS = 5000;
const recentSent = new Map(); // key -> timestamp

function _tenantId() {
  try {
    const auth = JSON.parse(localStorage.getItem("auth") || "{}");
    return auth?.tenant?.id || auth?.tenantId || "";
  } catch {
    return "";
  }
}

function _shouldSend(key) {
  const now = Date.now();
  // Clear expired
  for (const [k, ts] of recentSent) {
    if (now - ts > RECENT_WINDOW_MS) recentSent.delete(k);
  }
  if (recentSent.has(key)) return false;
  recentSent.set(key, now);
  return true;
}

function _notifyUser(kind, detail) {
  // User-visible surface for runtime errors that would otherwise leave the
  // view in a silent broken state. Only fires for error classes the user
  // can act on — not console.warn spam, not deprecation notices. App.vue
  // listens for `gdx-runtime-error` and shows a PrimeVue toast.
  if (kind !== "unhandled_rejection" && kind !== "vue_error" && kind !== "window_error") {
    return;
  }
  try {
    window.dispatchEvent(
      new CustomEvent("gdx-runtime-error", {
        detail: { kind, message: String(detail).slice(0, 200) },
      }),
    );
  } catch { /* ignore — telemetry still fires below */ }
}

function _send(kind, detail, extra = {}) {
  const key = `${kind}:${String(detail).slice(0, 200)}`;
  if (!_shouldSend(key)) return;
  _notifyUser(kind, detail);
  try {
    fetch("/api/feedback/client-error", {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-tenant-id": _tenantId() },
      // keepalive lets this survive a navigation
      keepalive: true,
      body: JSON.stringify({
        kind,
        detail: String(detail).slice(0, 2000),
        page: window.location.pathname,
        user_agent: navigator.userAgent.slice(0, 200),
        timestamp: new Date().toISOString(),
        ...extra,
      }),
    }).catch(() => {});
  } catch {}
}

export function installErrorCapture(app) {
  // ── Uncaught JS errors ──────────────────────────────────────────────────
  window.addEventListener("error", (event) => {
    _send("window_error", event.message || String(event.error), {
      source: event.filename,
      lineno: event.lineno,
      colno: event.colno,
      stack: event.error?.stack?.slice(0, 2000),
    });
  });

  // ── Unhandled promise rejections ─────────────────────────────────────────
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    const detail = reason?.message || reason?.toString?.() || String(reason);
    _send("unhandled_rejection", detail, {
      stack: reason?.stack?.slice(0, 2000),
    });
  });

  // ── Vue component errors ────────────────────────────────────────────────
  const prevErrorHandler = app.config.errorHandler;
  app.config.errorHandler = (err, instance, info) => {
    _send("vue_error", err?.message || String(err), {
      info,
      component: instance?.$options?.name || instance?.type?.name || "unknown",
      stack: err?.stack?.slice(0, 2000),
    });
    if (prevErrorHandler) prevErrorHandler(err, instance, info);
  };

  // ── Vue warning capture (dev + prod) ────────────────────────────────────
  // Deprecation warnings like "Deprecated since v4. Use Tabs component"
  // come through app.config.warnHandler in dev builds. In prod they show
  // up as console.warn. Wrap console.warn to forward those too.
  const prevWarnHandler = app.config.warnHandler;
  app.config.warnHandler = (msg, instance, trace) => {
    _send("vue_warning", msg, { trace: String(trace).slice(0, 1000) });
    if (prevWarnHandler) prevWarnHandler(msg, instance, trace);
  };

  const origConsoleError = console.error;
  console.error = function (...args) {
    try {
      const first = args[0];
      // Skip our own duplicate forwards and known-benign ones
      const detail = args.map((a) => {
        if (a instanceof Error) return a.message;
        if (typeof a === "object") return JSON.stringify(a).slice(0, 500);
        return String(a);
      }).join(" ");
      // Already forwarded by useApi.js: don't double-count API errors.
      if (!detail.startsWith("API ") && !detail.includes("→ 4") && !detail.includes("→ 5")) {
        _send("console_error", detail);
      }
    } catch {}
    return origConsoleError.apply(console, args);
  };

  const origConsoleWarn = console.warn;
  console.warn = function (...args) {
    try {
      const detail = args.map((a) => (a instanceof Error ? a.message : typeof a === "object" ? JSON.stringify(a).slice(0, 300) : String(a))).join(" ");
      // PrimeVue deprecation warnings are worth tracking
      if (detail.includes("Deprecated") || detail.includes("deprecated")) {
        _send("deprecation_warning", detail);
      }
    } catch {}
    return origConsoleWarn.apply(console, args);
  };
}
