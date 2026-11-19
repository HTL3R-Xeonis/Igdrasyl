"use strict";

// "Connection lost": tells a person clearly WHICH layer is down (browser -> gateway -> server of the selected host -> host)
// instead of leaving the canvas to fail quietly. Client-side only on purpose: it has to keep working when the server it
// watches is gone. The logic (classification, back-off, the state machine) is plain functions so tests/js can run it with
// node; the DOM part at the bottom only draws what the machine decides.

(function (root) {
  const HEARTBEAT_MS = 20000; // an idle, visible tab notices an outage within this time
  const CHECK_TIMEOUT_MS = 5000;
  const CONFIRM_DELAY_MS = 3000; // two failed checks this far apart before the overlay opens (no false alarm on one slow call)
  const BACKOFF_MS = [2000, 4000, 8000, 15000];
  const RESTORED_SHOWN_MS = 3000;

  // 2 s, 4 s, 8 s, then 15 s for every further try.
  function backoffMs(failures) {
    const index = Math.min(Math.max(failures, 1) - 1, BACKOFF_MS.length - 1);
    return BACKOFF_MS[index];
  }

  // Only an answer that is NOT the app's own JSON counts as an outage: a 503 with a JSON body comes from a reachable
  // server that reports a content problem (the template service, for example) and stays the job of its own banner.
  function classifyResponse(status, contentType) {
    if (status === 401) return "session";
    if ((status === 502 || status === 503 || status === 504) && !/json/i.test(contentType || "")) return "outage";
    return "app";
  }

  // One GET with a timeout, never throws: { ok, status, json, contentType, error }.
  function makeGetter(fetchImpl, setTimeoutImpl, clearTimeoutImpl) {
    return async function get(url) {
      const controller = typeof AbortController === "function" ? new AbortController() : null;
      const timer = controller ? setTimeoutImpl(() => controller.abort(), CHECK_TIMEOUT_MS) : null;
      try {
        const response = await fetchImpl(url, {
          cache: "no-store",
          credentials: "same-origin",
          headers: { Accept: "application/json" },
          signal: controller ? controller.signal : undefined,
        });
        const contentType = response.headers.get("content-type") || "";
        let json = null;
        if (/json/i.test(contentType)) {
          try { json = await response.json(); } catch (err) { json = null; }
        }
        return { ok: response.ok, status: response.status, json, contentType, error: null };
      } catch (err) {
        return { ok: false, status: 0, json: null, contentType: "", error: String(err && err.name || err) };
      } finally {
        if (timer) clearTimeoutImpl(timer);
      }
    };
  }

  // Looks at every layer once. States per layer: "ok", "fail", "unknown" (not checked because a lower layer failed).
  async function runChecks(env) {
    const out = {
      browser: "ok", gateway: "unknown", backend: "unknown", host: "unknown",
      backendState: null, selected: null, hostAddress: null,
      session: true, singleProcess: false, version: null,
    };
    if (env.online() === false) {
      out.browser = "fail";
      return out;
    }
    const gateway = await env.get("/gateway/health");
    if (gateway.status === 404) {
      out.singleProcess = true; // no gateway in this deployment: the app itself is the first layer
    } else if (gateway.ok) {
      out.gateway = "ok";
      out.version = gateway.json && gateway.json.version ? gateway.json.version : null;
    } else {
      out.gateway = "fail";
      return out;
    }
    const backend = await env.get("/health");
    if (backend.status === 401) {
      out.session = false;
      return out;
    }
    if (backend.ok && backend.json) {
      out.backend = "ok";
      out.hostAddress = backend.json.esxi_ip || null;
      out.host = out.hostAddress ? "ok" : "unknown";
      if (out.singleProcess) out.version = backend.json.version || null;
      return out;
    }
    out.backend = "fail";
    if (!out.singleProcess) {
      const status = await env.get("/gateway/status");
      if (status.status === 401) {
        out.session = false;
      } else if (status.ok && status.json) {
        out.backendState = status.json.backend || null;
        out.selected = status.json.selected || null;
        out.hostAddress = status.json.host || null;
      }
    }
    return out;
  }

  function isHealthy(result) {
    return result.browser === "ok" && result.session && result.gateway !== "fail" && result.backend === "ok";
  }

  // The title, the one plain sentence and the four layers for what runChecks found.
  function describeSituation(result) {
    const name = result.selected || "the selected host";
    let title = "Connection lost";
    let message = "The Ginnungagap server is not answering. It may be restarting.";
    if (result.browser === "fail") {
      title = "No network connection";
      message = "This computer has no network connection to the Ginnungagap server.";
    } else if (result.gateway === "ok" && result.backend === "fail") {
      if (result.backendState === "starting") {
        title = `The server for ${name} is starting`;
        message = "It is not answering yet. This page keeps trying by itself.";
      } else if (result.backendState === "stopped") {
        title = `The server for ${name} is not running`;
        message = "Its process is stopped. An administrator can start it again, or choose another server.";
      } else if (result.backendState === "none") {
        title = "No server selected";
        message = "Choose a server to continue.";
      } else {
        title = `The server for ${name} is not reachable`;
        message = "The gateway is running, but this host's server does not answer.";
      }
    }
    const layers = [{ label: "Browser", state: result.browser }];
    if (!result.singleProcess) layers.push({ label: "Gateway", state: result.gateway });
    layers.push({ label: result.selected ? `Server (${result.selected})` : "Server", state: result.backend });
    layers.push({ label: result.hostAddress ? `Host (${result.hostAddress})` : "Host", state: result.host });
    return { title, message, layers, singleProcess: result.singleProcess, gatewayUp: result.gateway === "ok" };
  }

  // env: { get, online, visible, now, sleep, setTimeout, clearTimeout, setInterval, clearInterval, reload, goto, ui }
  // ui: { show(situation, info), tick(info), hide(), restored() } - all optional pieces of drawing.
  function createMonitor(env) {
    let state = "ok"; // ok | confirming | lost
    let failures = 0;
    let lostAt = 0;
    let nextAt = 0;
    let retryTimer = null;
    let tickTimer = null;
    let heartbeat = null;
    let expectUntil = 0;
    let checking = false;
    let knownVersion = null;
    let lastSituation = null;
    const restoredCallbacks = [];

    const suppressed = () => env.now() < expectUntil;

    function stopTimers() {
      if (retryTimer) { env.clearTimeout(retryTimer); retryTimer = null; }
      if (tickTimer) { env.clearInterval(tickTimer); tickTimer = null; }
    }

    function info() {
      return { lostAt, nextAt, now: env.now(), failures };
    }

    function handleSession() {
      stopTimers();
      state = "ok";
      env.ui.hide();
      env.goto("/login?expired=1&next=" + encodeURIComponent(env.location()));
    }

    function enterLost(result) {
      state = "lost";
      failures = 1;
      lostAt = env.now();
      lastSituation = describeSituation(result);
      scheduleRetry();
      env.ui.show(lastSituation, info());
      if (tickTimer) env.clearInterval(tickTimer);
      tickTimer = env.setInterval(() => env.ui.tick(info()), 1000);
    }

    function scheduleRetry() {
      nextAt = env.now() + backoffMs(failures);
      if (retryTimer) env.clearTimeout(retryTimer);
      retryTimer = env.setTimeout(retry, backoffMs(failures));
    }

    async function retry() {
      if (state !== "lost" || checking) return;
      checking = true;
      let result;
      try { result = await runChecks(env); } finally { checking = false; }
      if (isHealthy(result)) return restore(result);
      if (!result.session) return handleSession();
      failures += 1;
      lastSituation = describeSituation(result);
      scheduleRetry();
      env.ui.show(lastSituation, info());
    }

    function restore(result) {
      stopTimers();
      state = "ok";
      failures = 0;
      env.ui.hide();
      env.ui.restored();
      if (result && result.version && knownVersion && result.version !== knownVersion) {
        env.reload(); // the server was updated while it was away: take the new page
        return;
      }
      if (result && result.version && !knownVersion) knownVersion = result.version;
      for (const callback of restoredCallbacks) {
        try { callback(); } catch (err) { /* a failing callback must not block the others */ }
      }
    }

    // A request failed like an outage (network error, gateway 502/503/504 without JSON): check twice before saying so.
    async function confirmOutage() {
      checking = true;
      try {
        const first = await runChecks(env);
        if (isHealthy(first)) { state = "ok"; return; }
        if (!first.session) return handleSession();
        await env.sleep(CONFIRM_DELAY_MS);
        const second = await runChecks(env);
        if (isHealthy(second)) { state = "ok"; return; }
        if (!second.session) return handleSession();
        if (suppressed()) { state = "ok"; return; }
        enterLost(second);
      } finally {
        checking = false;
      }
    }

    function noteFailure() {
      if (state !== "ok" || checking || suppressed()) return;
      state = "confirming";
      confirmOutage();
    }

    function noteSession() {
      if (state === "lost") return;
      handleSession();
    }

    function retryNow() {
      if (state !== "lost") return;
      if (retryTimer) { env.clearTimeout(retryTimer); retryTimer = null; }
      retry();
    }

    async function beat() {
      if (state !== "ok" || checking || suppressed() || !env.visible()) return;
      checking = true;
      let result;
      try { result = await runChecks(env); } finally { checking = false; }
      if (result.version && !knownVersion) knownVersion = result.version;
      if (isHealthy(result)) return;
      if (!result.session) return handleSession();
      noteFailure();
    }

    // For the places that expect the server to go away for a moment (switching host in a single-process install): the
    // overlay stays quiet and this resolves when /health answers again, false after the timeout.
    async function waitUntilBack(timeoutMs = 30000, intervalMs = 1000) {
      expectUntil = env.now() + timeoutMs + 5000;
      const deadline = env.now() + timeoutMs;
      while (env.now() < deadline) {
        await env.sleep(intervalMs);
        const answer = await env.get("/health");
        if (answer.ok) { expectUntil = 0; return true; }
      }
      expectUntil = 0;
      return false;
    }

    function start() {
      if (heartbeat) return;
      heartbeat = env.setInterval(beat, HEARTBEAT_MS);
      beat();
    }

    return {
      noteFailure,
      noteSession,
      retryNow,
      waitUntilBack,
      start,
      onRestored(callback) { restoredCallbacks.push(callback); },
      expectOutage(ms) { expectUntil = env.now() + ms; },
      get state() { return state; },
      get situation() { return lastSituation; },
    };
  }

  const api = { backoffMs, classifyResponse, makeGetter, runChecks, isHealthy, describeSituation, createMonitor, HEARTBEAT_MS };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
    return;
  }
  if (typeof document === "undefined") return;

  // ---- the browser: wraps fetch once and draws the overlay ----

  const originalFetch = root.fetch.bind(root);
  const get = makeGetter(originalFetch, (fn, ms) => root.setTimeout(fn, ms), (id) => root.clearTimeout(id));
  const byId = (id) => document.getElementById(id);

  function formatAgo(seconds) {
    if (seconds < 60) return `${seconds} second${seconds === 1 ? "" : "s"} ago`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  }

  const STATE_WORDS = { ok: "reached", fail: "not reachable", unknown: "not checked" };
  const INERT_IDS = ["app-view", "landing-view", "template-banner", "ap"];

  const ui = {
    show(situation, details) {
      const overlay = byId("connection-lost");
      if (!overlay) return;
      byId("cl-title").textContent = situation.title;
      byId("cl-message").textContent = situation.message;
      const list = byId("cl-layers");
      list.textContent = "";
      for (const layer of situation.layers) {
        const item = document.createElement("li");
        item.className = `cl-layer cl-${layer.state}`;
        const dot = document.createElement("span");
        dot.className = "cl-dot";
        const label = document.createElement("span");
        label.textContent = layer.label;
        const word = document.createElement("span");
        word.className = "cl-state";
        word.textContent = STATE_WORDS[layer.state] || "";
        item.append(dot, label, word);
        list.append(item);
      }
      byId("cl-picker").hidden = situation.singleProcess;
      byId("cl-signout").hidden = !situation.gatewayUp;
      const wasHidden = overlay.hidden;
      overlay.hidden = false;
      for (const id of INERT_IDS) {
        const element = byId(id);
        if (element) element.inert = true;
      }
      this.tick(details);
      if (wasHidden) byId("cl-retry").focus();
    },
    tick(details) {
      const since = Math.max(0, Math.round((details.now - details.lostAt) / 1000));
      byId("cl-since").textContent = `Lost at ${new Date(details.lostAt).toLocaleTimeString()} (${formatAgo(since)})`;
      const left = Math.max(0, Math.ceil((details.nextAt - details.now) / 1000));
      byId("cl-countdown").textContent = left > 0 ? `Trying again in ${left} s` : "Trying again now";
    },
    hide() {
      const overlay = byId("connection-lost");
      if (overlay) overlay.hidden = true;
      for (const id of INERT_IDS) {
        const element = byId(id);
        if (element) element.inert = false;
      }
    },
    restored() {
      const note = byId("cl-restored");
      if (!note) return;
      note.hidden = false;
      root.setTimeout(() => { note.hidden = true; }, RESTORED_SHOWN_MS);
    },
  };

  const monitor = createMonitor({
    get,
    online: () => root.navigator.onLine,
    visible: () => document.visibilityState !== "hidden",
    now: () => Date.now(),
    sleep: (ms) => new Promise((resolve) => root.setTimeout(resolve, ms)),
    setTimeout: (fn, ms) => root.setTimeout(fn, ms),
    clearTimeout: (id) => root.clearTimeout(id),
    setInterval: (fn, ms) => root.setInterval(fn, ms),
    clearInterval: (id) => root.clearInterval(id),
    reload: () => root.location.reload(),
    goto: (url) => root.location.assign(url),
    location: () => root.location.pathname + root.location.search,
    ui,
  });

  // Every same-origin request passes through here once. Only the response HEAD is looked at (never the body), so
  // streaming downloads and uploads behave exactly as before.
  root.fetch = async function (input, init) {
    let sameOrigin = true;
    try {
      const url = new URL(typeof input === "string" ? input : (input && input.url) || String(input), root.location.href);
      sameOrigin = url.origin === root.location.origin;
    } catch (err) {
      sameOrigin = true;
    }
    try {
      const response = await originalFetch(input, init);
      if (sameOrigin) {
        const kind = classifyResponse(response.status, response.headers.get("content-type"));
        if (kind === "outage") monitor.noteFailure();
        else if (kind === "session") monitor.noteSession();
      }
      return response;
    } catch (err) {
      const aborted = err && err.name === "AbortError";
      if (sameOrigin && !aborted) monitor.noteFailure();
      throw err;
    }
  };

  root.addEventListener("offline", () => monitor.noteFailure());
  root.addEventListener("online", () => monitor.retryNow());
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") monitor.start();
  });
  byId("cl-retry").addEventListener("click", () => monitor.retryNow());
  monitor.onRestored(() => {
    if (typeof root.loadCanvas === "function") root.loadCanvas();
  });
  root.connectionMonitor = monitor;
  monitor.start();
})(typeof window !== "undefined" ? window : globalThis);
