"use strict";
// Run with: node --test tests/js   (tests/test_connection_monitor_js.py does that inside pytest when node is installed)
const test = require("node:test");
const assert = require("node:assert/strict");
const m = require("../../app/static/connection-monitor.js");

const flush = () => new Promise((resolve) => setImmediate(resolve));

// A fake world: a manual clock, timers that only run when advance() says so, and scripted answers per URL.
function world(answers) {
  let now = 1_000_000;
  let nextId = 1;
  const timers = [];
  const calls = [];
  const log = { show: [], hide: 0, restored: 0, goto: [], reload: 0, tick: 0, callbacks: 0 };
  const state = { online: true, visible: true, answers: { ...answers } };
  const env = {
    get: async (url) => {
      calls.push(url);
      const answer = state.answers[url];
      const value = typeof answer === "function" ? answer() : answer;
      return value || { ok: false, status: 0, json: null, contentType: "", error: "TypeError" };
    },
    online: () => state.online,
    visible: () => state.visible,
    now: () => now,
    sleep: (ms) => new Promise((resolve) => timers.push({ id: nextId++, at: now + ms, fn: resolve })),
    setTimeout: (fn, ms) => { const id = nextId++; timers.push({ id, at: now + ms, fn }); return id; },
    clearTimeout: (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); },
    setInterval: (fn, ms) => { const id = nextId++; timers.push({ id, at: now + ms, fn, every: ms }); return id; },
    clearInterval: (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); },
    reload: () => { log.reload += 1; },
    goto: (url) => { log.goto.push(url); },
    location: () => "/canvas?x=1",
    ui: {
      show: (situation) => log.show.push(situation),
      tick: () => { log.tick += 1; },
      hide: () => { log.hide += 1; },
      restored: () => { log.restored += 1; },
    },
  };
  async function advance(ms) {
    const target = now + ms;
    await flush(); // let the checks that were just started register their timers
    await flush();
    for (;;) {
      timers.sort((a, b) => a.at - b.at);
      const due = timers.find((t) => t.at <= target);
      if (!due) break;
      now = due.at;
      if (due.every) due.at += due.every; else timers.splice(timers.indexOf(due), 1);
      due.fn();
      await flush();
      await flush();
    }
    now = target;
    await flush();
  }
  return { env, state, log, advance, calls };
}

const ok = (json) => ({ ok: true, status: 200, json, contentType: "application/json", error: null });
const status = (code, json = null, contentType = "application/json") => ({ ok: code < 400, status: code, json, contentType, error: null });
const down = { ok: false, status: 0, json: null, contentType: "", error: "TypeError" };
const healthy = () => ({
  "/gateway/health": ok({ status: "ok", version: "1.1.0" }),
  "/health": ok({ esxi_ip: "10.0.0.5" }),
  "/connection/host": ok({ host: "10.0.0.5", status: "ok" }),
  "/gateway/status": ok({ selected: "lab.yml", backend: "running", host: "10.0.0.5" }),
});

test("an ESXi outage is detected while the gateway and backend still answer", async () => {
  const w = world({ ...healthy(), "/connection/host": ok({ host: "10.0.0.5", status: "unreachable" }) });
  const monitor = m.createMonitor(w.env);
  monitor.start();
  await w.advance(4000);
  assert.equal(monitor.state, "lost");
  const result = await m.runChecks(w.env);
  assert.equal(result.backend, "ok");
  assert.equal(result.host, "fail");
  assert.equal(m.isHealthy(result), false);
  assert.equal(m.describeSituation(result).title, "Connection to host 10.0.0.5 lost");
  w.state.answers["/connection/host"] = ok({ host: "10.0.0.5", status: "ok" });
  await w.advance(5000);
  assert.equal(monitor.state, "ok");
});

test("the back-off is 2 s, 4 s, 8 s and then 15 s for every further try", () => {
  assert.deepEqual([1, 2, 3, 4, 5, 9].map(m.backoffMs), [2000, 4000, 8000, 15000, 15000, 15000]);
  assert.equal(m.backoffMs(0), 2000);
});

test("only an answer without the app's own JSON counts as an outage", () => {
  assert.equal(m.classifyResponse(502, "text/plain; charset=utf-8"), "outage");
  assert.equal(m.classifyResponse(504, "text/html"), "outage");
  assert.equal(m.classifyResponse(503, "application/json"), "app"); // a reachable server reporting a content problem
  assert.equal(m.classifyResponse(409, "application/json"), "app");
  assert.equal(m.classifyResponse(500, "application/json"), "app");
  assert.equal(m.classifyResponse(401, "application/json"), "session");
  assert.equal(m.classifyResponse(200, ""), "app");
});

test("the layers: a dead gateway stops the check there", async () => {
  const w = world({ "/gateway/health": down });
  const result = await m.runChecks(w.env);
  assert.equal(result.gateway, "fail");
  assert.equal(result.backend, "unknown");
  assert.deepEqual(w.calls, ["/gateway/health"]);
  assert.equal(m.isHealthy(result), false);
});

test("a stopped backend is read from the gateway status", async () => {
  const w = world({ ...healthy(), "/health": status(409, { detail: "No backend selected or it is not running yet." }), "/gateway/status": ok({ selected: "lab.yml", backend: "stopped", host: "10.0.0.5" }) });
  const result = await m.runChecks(w.env);
  assert.equal(result.gateway, "ok");
  assert.equal(result.backend, "fail");
  assert.equal(result.backendState, "stopped");
  const situation = m.describeSituation(result);
  assert.equal(situation.title, "The server for lab.yml is not running");
  assert.deepEqual(situation.layers.map((l) => l.state), ["ok", "ok", "fail", "unknown"]);
});

test("the titles for every situation", async () => {
  const base = { browser: "ok", gateway: "ok", backend: "fail", host: "unknown", selected: "lab.yml", hostAddress: null, session: true, singleProcess: false };
  assert.equal(m.describeSituation({ ...base, browser: "fail", gateway: "unknown", backend: "unknown" }).title, "No network connection");
  assert.equal(m.describeSituation({ ...base, gateway: "fail", backend: "unknown" }).title, "Connection lost");
  assert.equal(m.describeSituation({ ...base, backendState: "starting" }).title, "The server for lab.yml is starting");
  assert.equal(m.describeSituation({ ...base, backendState: "none", selected: null }).title, "No server selected");
  assert.equal(m.describeSituation({ ...base, backendState: null }).title, "The server for lab.yml is not reachable");
  const single = m.describeSituation({ ...base, singleProcess: true, gateway: "unknown", backend: "fail", selected: null });
  assert.equal(single.layers.length, 3); // no gateway layer without a gateway
  assert.equal(single.title, "Connection lost");
});

test("a single-process install without a gateway checks the server itself", async () => {
  const w = world({ "/gateway/health": status(404), "/health": ok({ esxi_ip: null, version: "1.1.0" }) });
  const result = await m.runChecks(w.env);
  assert.equal(result.singleProcess, true);
  assert.equal(m.isHealthy(result), true);
  assert.equal(result.version, "1.1.0");
});

test("offline is detected without any request", async () => {
  const w = world(healthy());
  w.state.online = false;
  const result = await m.runChecks(w.env);
  assert.equal(result.browser, "fail");
  assert.deepEqual(w.calls, []);
});

test("one slow call is no outage: the second check is fine and nothing is shown", async () => {
  const w = world(healthy());
  const monitor = m.createMonitor(w.env);
  let count = 0;
  w.state.answers["/gateway/health"] = () => (++count === 1 ? down : ok({ status: "ok", version: "1.1.0" }));
  monitor.noteFailure();
  await w.advance(4000);
  assert.equal(w.log.show.length, 0);
  assert.equal(monitor.state, "ok");
});

test("two failed checks open the overlay, it retries with back-off and recovers once", async () => {
  const w = world({ "/gateway/health": down });
  const monitor = m.createMonitor(w.env);
  monitor.onRestored(() => { w.log.callbacks += 1; });
  monitor.noteFailure();
  await w.advance(3500); // first check, 3 s wait, second check
  assert.equal(w.log.show.length, 1);
  assert.equal(w.log.show[0].title, "Connection lost");
  assert.equal(monitor.state, "lost");
  const before = w.calls.length;
  await w.advance(2000); // first back-off
  assert.ok(w.calls.length > before, "a retry ran after 2 s");
  assert.ok(w.log.tick > 0, "the countdown is redrawn every second");
  Object.assign(w.state.answers, healthy());
  await w.advance(15000);
  assert.equal(monitor.state, "ok");
  assert.equal(w.log.hide, 1);
  assert.equal(w.log.restored, 1);
  assert.equal(w.log.callbacks, 1);
});

test("a new server version after the outage reloads the page once instead of patching it", async () => {
  const w = world(healthy());
  const monitor = m.createMonitor(w.env);
  monitor.start();
  await w.advance(100); // the first heartbeat learns version 1.1.0
  w.state.answers["/gateway/health"] = down;
  monitor.noteFailure();
  await w.advance(3500);
  assert.equal(monitor.state, "lost");
  w.state.answers["/gateway/health"] = ok({ status: "ok", version: "1.2.0" });
  w.state.answers["/health"] = ok({ esxi_ip: "10.0.0.5" });
  await w.advance(20000);
  assert.equal(w.log.reload, 1);
});

test("an idle hidden tab does not poll", async () => {
  const w = world(healthy());
  const monitor = m.createMonitor(w.env);
  w.state.visible = false;
  monitor.start();
  await w.advance(60000);
  assert.deepEqual(w.calls, []);
  w.state.visible = true;
  await w.advance(20000);
  assert.ok(w.calls.length > 0);
});

test("a session that ended goes to the login page and comes back to the same place", async () => {
  const w = world({ ...healthy(), "/health": status(401) });
  const monitor = m.createMonitor(w.env);
  monitor.noteFailure();
  await w.advance(100);
  assert.deepEqual(w.log.goto, ["/login?expired=1&next=%2Fcanvas%3Fx%3D1"]);
  assert.equal(w.log.show.length, 0);
});

test("while a restart is expected the overlay stays quiet and waitUntilBack resolves when the server answers", async () => {
  const w = world({ "/gateway/health": down, "/health": down });
  const monitor = m.createMonitor(w.env);
  const waiting = monitor.waitUntilBack(30000, 1000);
  monitor.noteFailure(); // ignored: the outage is expected
  await w.advance(5000);
  assert.equal(w.log.show.length, 0);
  w.state.answers["/health"] = ok({});
  await w.advance(2000);
  assert.equal(await waiting, true);
});

test("waitUntilBack gives up after the timeout", async () => {
  const w = world({ "/health": down });
  const monitor = m.createMonitor(w.env);
  const waiting = monitor.waitUntilBack(5000, 1000);
  await w.advance(8000);
  assert.equal(await waiting, false);
});

test("retry now checks immediately", async () => {
  const w = world({ "/gateway/health": down });
  const monitor = m.createMonitor(w.env);
  monitor.noteFailure();
  await w.advance(3500);
  const before = w.calls.length;
  monitor.retryNow();
  await w.advance(10);
  assert.ok(w.calls.length > before);
});
