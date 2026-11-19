"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const projects = fs.readFileSync("app/static/projects.js", "utf8");
const status = fs.readFileSync("app/static/palette-and-screenshot.js", "utf8");
const host = fs.readFileSync("app/static/connection-settings-and-maintenance.js", "utf8");
const source = [
  status.slice(status.indexOf("const MENUBAR_ERROR_PATTERN")),
  projects.slice(projects.indexOf("async function monitorLibraryDeployment("), projects.indexOf("async function startLibraryDeployment(")),
  host.slice(host.indexOf("async function openLiveCanvas("), host.indexOf("// Gateway deployment:")),
].join("\n");

function setup(designMode, result) {
  function element() {
    return {
      children: [], classList: { toggle() {}, remove() {} },
      appendChild(child) { this.children.push(child); child.parent = this; },
      replaceChildren() { this.children = []; },
      setAttribute() {},
      addEventListener(_event, callback) { this.click = callback; },
      remove() { this.parent.children = this.parent.children.filter(child => child !== this); },
    };
  }
  const elements = new Map();
  const calls = [];
  const context = vm.createContext({
    designMode,
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
      },
      createElement: element,
    },
    setMenubarBusy() {},
    pollImportJob: async id => { calls.push(id); return result; },
    loadCanvas: async () => calls.push(context.designMode ? "design" : "live"),
    renderPushResults() {},
    applyDesignModeUI() {},
    setWarningBanner: () => assert.fail("unexpected warning"),
  });
  vm.runInContext(source, context);
  return { context, calls, elements, buttons: () => elements.get("menubar-status").children.filter(child => child.type === "button" && child.className !== "menubar-dismiss") };
}

for (const push_results of [undefined, [{ ok: false }]]) {
  test(`successful design deployment offers a live switch (push failures: ${Boolean(push_results)})`, async () => {
    const app = setup(true, { status: "done", push_results });
    await app.context.monitorLibraryDeployment({ name: "Lab" }, "job1");
    assert.equal(app.context.designMode, true, "keep the designer open until the user clicks");
    assert.deepEqual(app.calls, ["job1", "design"]);
    const [button] = app.buttons();
    assert.equal(button.textContent, "Open live mode");
    await button.click();
    assert.equal(app.context.designMode, false);
    assert.deepEqual(app.calls, ["job1", "design", "live"]);
    assert.equal(app.elements.get("landing-view").hidden, true);
    assert.equal(app.elements.get("app-view").hidden, false);
    assert.equal(app.buttons().length, 0);
  });
}

test("a failed deployment offers no live switch", async () => {
  const app = setup(true, { status: "error", error: "host disconnected" });
  await app.context.monitorLibraryDeployment({ name: "Lab" }, "job1");
  assert.equal(app.buttons().length, 0);
  assert.equal(app.context.designMode, true);
  assert.deepEqual(app.calls, ["job1"]);
});

test("a deployment already in live mode offers no redundant switch", async () => {
  const app = setup(false, { status: "done" });
  await app.context.monitorLibraryDeployment({ name: "Lab" }, "job1");
  assert.equal(app.buttons().length, 0);
  assert.deepEqual(app.calls, ["job1", "live"]);
});
