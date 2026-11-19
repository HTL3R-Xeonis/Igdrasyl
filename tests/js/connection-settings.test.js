"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("app/static/connection-settings-and-maintenance.js", "utf8");
const handler = source.slice(
  source.indexOf('connectionApply.addEventListener("click"'),
  source.indexOf("// Opens the live canvas")
);

test("applying saved gateway credentials restarts the backend before switching", async () => {
  const calls = [];
  let apply;
  vm.runInNewContext(handler, {
    connectionApply: { addEventListener: (_event, callback) => { apply = callback; } },
    connectionDialog: { close: () => calls.push("close") },
    connectionFilename: "host.yml",
    isGatewayMode: async () => true,
    settingsRequest: async (url, options) => calls.push([url, options.method]),
    switchGatewayHost: async filename => calls.push(filename),
    encodeURIComponent,
    showAlert: async () => assert.fail("unexpected failure"),
  });
  await apply();
  assert.deepEqual(calls, ["close", ["/settings-profiles/host.yml/restart", "POST"], "host.yml"]);
});

test("a failed credential apply displays an error and does not switch", async () => {
  let apply;
  let error;
  vm.runInNewContext(handler, {
    connectionApply: { addEventListener: (_event, callback) => { apply = callback; } },
    connectionDialog: { close() {} },
    connectionFilename: "host.yml",
    isGatewayMode: async () => true,
    settingsRequest: async () => { throw new Error("restart failed"); },
    switchGatewayHost: async () => assert.fail("must not switch after failed restart"),
    encodeURIComponent,
    showAlert: async message => { error = message; },
  });
  await apply();
  assert.match(error, /restart failed/);
});

const projectsSource = fs.readFileSync("app/static/projects.js", "utf8");
const restartProject = projectsSource.slice(
  projectsSource.indexOf("async function restartIntoProject("),
  projectsSource.indexOf('document.getElementById("deploy-library-project-btn")')
);

for (const failRestart of [false, true]) {
  test(`preparing a gateway project restarts its backend (failure: ${failRestart})`, async () => {
    const calls = [];
    const pending = new Map();
    const context = vm.createContext({
      currentDesignProject: { id: "project1" },
      settingsRequest: async url => {
        calls.push(url);
        if (failRestart && url.endsWith("/restart")) throw new Error("restart failed");
      },
      sessionStorage: {
        setItem: (key, value) => pending.set(key, value),
        removeItem: key => pending.delete(key),
      },
      deploymentDialog: { open: false },
      setMenubarBusy() {},
      isGatewayMode: async () => true,
      window: { location: { reload: () => calls.push("reload") } },
      encodeURIComponent,
    });
    vm.runInContext(restartProject, context);
    const work = context.restartIntoProject({ host: { filename: "host.yml" } });
    if (failRestart) {
      await assert.rejects(work, /restart failed/);
      assert.deepEqual(calls, ["/settings-profiles/host.yml/project", "/settings-profiles/host.yml/restart"]);
      assert.equal(pending.has("pendingLibraryDeployment"), false);
    } else {
      await work;
      assert.deepEqual(calls, [
        "/settings-profiles/host.yml/project", "/settings-profiles/host.yml/restart",
        "/settings-profiles/host.yml/activate", "reload",
      ]);
      assert.equal(pending.has("pendingLibraryDeployment"), true);
    }
  });
}
