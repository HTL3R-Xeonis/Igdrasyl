"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("app/static/import-and-generate.js", "utf8");
const handler = source.slice(source.indexOf("let exportingGns3Project"), source.indexOf('document.getElementById("deploy-gns3-vm-btn")'));

function setup({ confirmed = true, status = 200, networkError = false } = {}) {
  const calls = [];
  const context = vm.createContext({
    document: {
      getElementById: () => ({ addEventListener() {} }),
      body: { appendChild() {} },
      createElement: () => ({
        click() { calls.push(["download", this.href, this.download]); },
        remove() {},
      }),
    },
    showConfirm: async message => { calls.push(["confirm", message]); return confirmed; },
    showAlert: async message => calls.push(["alert", message]),
    setMenubarBusy() {},
    setMenubarMessage() {},
    fetch: async url => {
      calls.push(["fetch", url]);
      if (networkError) throw new Error("connection lost");
      return {
        ok: status === 200, status,
        json: async () => ({ detail: "Stop all GNS3 devices first." }),
        blob: async () => ({ archive: true }),
        headers: { get: () => 'attachment; filename="Office_East.gns3project"' },
      };
    },
    URL: { createObjectURL: () => "blob:archive", revokeObjectURL() {} },
    setTimeout() {},
  });
  vm.runInContext(handler, context);
  return { calls, run: () => context.exportNativeGns3Project({ preventDefault: () => calls.push(["prevent-navigation"]) }) };
}

test("export warns before requesting an archive and cancellation does nothing", async () => {
  const app = setup({ confirmed: false });
  await app.run();
  assert.equal(app.calls[0][0], "prevent-navigation");
  assert.match(app.calls[1][1], /All devices.*must be stopped/);
  assert.equal(app.calls.length, 2);
});

test("confirming exports the archive with the server's filename", async () => {
  const app = setup();
  await app.run();
  assert.equal(app.calls[1][0], "confirm");
  assert.deepEqual(app.calls.slice(2), [
    ["fetch", "/export/gns3project"],
    ["download", "blob:archive", "Office_East.gns3project"],
  ]);
});

for (const options of [{ status: 409 }, { networkError: true }]) {
  test(`export errors stay in the UI and permit another attempt (${JSON.stringify(options)})`, async () => {
    const app = setup(options);
    await app.run();
    assert.equal(app.calls.some(call => call[0] === "download"), false);
    assert.equal(app.calls.at(-1)[0], "alert");
    await app.run();
    assert.equal(app.calls.filter(call => call[0] === "fetch").length, 2);
  });
}
