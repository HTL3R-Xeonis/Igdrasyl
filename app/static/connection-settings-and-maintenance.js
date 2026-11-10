// [connection-settings-and-maintenance.js] Host connection settings, health check, orphans, the connectivity test and the VM screen. Loaded in the order listed in index.html; all scripts share one global scope.
let connectionLoaded = false;
let connectionGroups = [];
let connectionPreviewTimer = null;
let connectionDirty = false;
const connectionDialog = document.getElementById("connection-dialog");
const connectionForm = document.getElementById("connection-form");
const connectionResults = document.getElementById("connection-results");
const connectionApply = document.getElementById("connection-apply");
const connectionFields = document.getElementById("connection-fields");

async function settingsRequest(url, options = {}) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${response.status})`);
  return body;
}
function connectionRequest(suffix = "", options = {}) {
  return settingsRequest(`/settings-profiles/${encodeURIComponent(connectionFilename)}/connections${suffix}`, options);
}
function settingsOptions(fields, extra = {}) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fields, ...extra }) };
}
// Entered passwords use field names "secret.<key>" and travel separately from
// the YAML fields - the server stores them apart from the profile file and
// never sends a value back (only whether one is stored).
const SECRET_PREFIX = "secret.";
const SECRET_FIELDS = [
  ["esxi_password", "ESXi password", "Password of the ESXi account above."],
  ["gns3_password", "GNS3 password", "Password of the GNS3 account in the GNS3 section."],
];
function settingsEntries() { return [...new FormData(connectionForm)]; }
function settingsValues() {
  return Object.fromEntries(settingsEntries().filter(([name]) => !name.startsWith(SECRET_PREFIX)));
}
function settingsSecrets() {
  return Object.fromEntries(settingsEntries()
    .filter(([name, value]) => name.startsWith(SECRET_PREFIX) && value)
    .map(([name, value]) => [name.slice(SECRET_PREFIX.length), value]));
}
function setPasswordHints(stored) {
  for (const [key] of SECRET_FIELDS) {
    const input = connectionForm.elements[SECRET_PREFIX + key];
    if (!input) continue;
    input.value = "";
    input.placeholder = stored?.[key] ? "A password is stored - leave empty to keep it" : "Not set - paste it here";
  }
}

async function openConnectionSettings(filename = null) {
  connectionFilename = filename;
  connectionLoaded = false;
  connectionDirty = false;
  connectionResults.textContent = "Loading settings…";
  connectionApply.hidden = true;
  document.getElementById("connection-yaml").textContent = "";
  document.getElementById("connection-title").textContent = filename ? `Settings — ${filename}` : "Design a host configuration";
  document.getElementById("connection-name-wrap").hidden = !!filename;
  document.getElementById("connection-name").value = "my-host.yml";
  connectionFields.replaceChildren();
  connectionDialog.showModal();
  setConnectionBusy(true);
  try {
    const [schema, body, ovaCatalog] = await Promise.all([
      settingsRequest("/host-config/schema"),
      filename ? connectionRequest() : Promise.resolve({ fields: {} }),
      settingsRequest("/ova-templates").catch(error => ({ templates: [], error: error.message })),
    ]);
    connectionGroups = schema.groups;
    for (const [index, group] of connectionGroups.entries()) {
      const section = document.createElement("fieldset");
      section.id = `connection-section-${index}`;
      const legend = document.createElement("legend");
      legend.textContent = group.title;
      section.appendChild(legend);
      for (const field of group.fields) {
        const label = document.createElement("label");
        const title = document.createElement("span");
        title.textContent = field.key.split(".").pop().replaceAll("_", " ").replace(/\b(esxi|gns3|api|vm|ova|mac|ip)\b/g, word => word.toUpperCase());
        label.appendChild(title);
        const input = document.createElement(field.type === "boolean" || field.type === "ova-template" ? "select" : field.type === "list" ? "textarea" : "input");
        if (field.type === "boolean") {
          for (const [value, text] of [["true", "Enabled"], ["false", "Disabled"]]) {
            const option = document.createElement("option"); option.value = value; option.textContent = text; input.appendChild(option);
          }
        } else if (field.type === "ova-template") {
          input.appendChild(new Option(ovaCatalog.templates.length ? "No GNS3 VM template selected" : "No GNS3 VM templates available", ""));
          for (const template of ovaCatalog.templates) {
            const details = [template.category, template.size].filter(Boolean).join(" · ");
            input.appendChild(new Option(details ? `${template.name} (${details})` : template.name, template.id));
          }
          const selected = body.fields[field.key] ?? field.default;
          if (selected && ![...input.options].some(option => option.value === selected)) {
            input.appendChild(new Option("Selected template is unavailable", selected));
          }
        } else if (field.type === "list") input.rows = 3;
        else {
          input.type = field.type;
          if (field.type === "number") { input.min = field.key.endsWith(".port") ? "1" : "0"; input.step = "1"; if (field.key.endsWith(".port")) input.max = "65535"; }
        }
        input.name = field.key;
        input.value = body.fields[field.key] ?? field.default;
        input.required = !field.optional;
        input.placeholder = field.optional ? "Automatic / not configured" : field.default;
        label.appendChild(input);
        const help = document.createElement("small");
        help.textContent = field.help || field.key;
        label.appendChild(help);
        section.appendChild(label);
      }
      connectionFields.appendChild(section);
    }
    connectionFields.appendChild(buildCredentialsSection());
    setPasswordHints(body.passwords_set);
    connectionLoaded = true;
    connectionResults.textContent = "";
    updateSettingsActions();
    await refreshSettingsPreview();
  } catch (error) { connectionResults.textContent = error.message; }
  finally { setConnectionBusy(false); }
}
function buildCredentialsSection() {
  const section = document.createElement("fieldset");
  section.id = "connection-section-credentials";
  const legend = document.createElement("legend");
  legend.textContent = "Credentials";
  section.appendChild(legend);
  for (const [key, title, help] of SECRET_FIELDS) {
    const label = document.createElement("label");
    const text = document.createElement("span");
    text.textContent = title;
    const input = document.createElement("input");
    input.type = "password";
    input.name = SECRET_PREFIX + key;
    input.autocomplete = "new-password";
    input.spellcheck = false;
    const note = document.createElement("small");
    note.textContent = `${help} Stored on the server only, never shown again.`;
    label.append(text, input, note);
    section.appendChild(label);
  }
  return section;
}
function updateSettingsActions() {
  document.getElementById("connection-save").textContent = connectionFilename ? "Save settings" : "Create host";
}
async function refreshSettingsPreview() {
  const target = document.getElementById("connection-yaml");
  try {
    target.textContent = (await settingsRequest("/host-config/preview", settingsOptions(settingsValues()))).yaml;
  } catch (error) {
    target.textContent = `The YAML can't be shown yet: ${error.message}`;
  }
}
function setConnectionBusy(busy) {
  connectionBusy = busy;
  connectionDialog.querySelectorAll("button, input, textarea, select").forEach(el => { el.disabled = busy || (!connectionLoaded && el.id !== "connection-close"); });
}
connectionForm.addEventListener("input", () => {
  clearTimeout(connectionPreviewTimer);
  connectionPreviewTimer = setTimeout(refreshSettingsPreview, 400);
  connectionDirty = true;
  connectionApply.hidden = true;
  connectionResults.textContent = "Unsaved changes. Connection results must be checked again after changes.";
});
connectionForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (!connectionForm.reportValidity()) return;
  // Read before setConnectionBusy: disabled inputs are left out of FormData.
  const fields = settingsValues();
  const secrets = settingsSecrets();
  const filename = document.getElementById("connection-name").value.trim();
  if (!connectionFilename && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.ya?ml$/.test(filename)) {
    connectionResults.textContent = "Use a filename ending in .yml or .yaml, with letters, numbers, dots, hyphens or underscores."; return;
  }
  setConnectionBusy(true);
  try {
    if (connectionFilename) {
      await connectionRequest("", { ...settingsOptions(fields, { secrets }), method: "PUT" });
      connectionResults.textContent = "Saved. Apply / restart when ready. Other settings are retained; YAML comments are removed.";
      setPasswordHints((await connectionRequest()).passwords_set);
      connectionApply.hidden = false;
    } else {
      await settingsRequest("/host-config/create", settingsOptions(fields, { filename, secrets }));
      connectionFilename = filename;
      document.getElementById("connection-title").textContent = `Settings — ${filename}`;
      document.getElementById("connection-name-wrap").hidden = true;
      updateSettingsActions();
      setPasswordHints((await connectionRequest()).passwords_set);
      connectionResults.textContent = "Host created. It is available on the home screen. Apply / restart to activate it.";
      connectionApply.hidden = false;
    }
    connectionDirty = false;
    await loadLandingHostGrid();
  } catch (error) { connectionResults.textContent = error.message; }
  finally { setConnectionBusy(false); }
});
document.getElementById("connection-download").addEventListener("click", () => {
  const name = connectionFilename || document.getElementById("connection-name").value.trim() || "settings.yml";
  const url = URL.createObjectURL(new Blob([document.getElementById("connection-yaml").textContent], { type: "application/yaml" }));
  const link = document.createElement("a"); link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
document.getElementById("connection-test").addEventListener("click", async () => {
  if (!connectionForm.reportValidity()) return;
  const fields = settingsValues();
  setConnectionBusy(true);
  connectionResults.textContent = "Checking services from the server…";
  try {
    const options = settingsOptions(fields);
    const { results } = await (connectionFilename ? connectionRequest("/test", options) : settingsRequest("/host-config/test", options));
    connectionResults.replaceChildren();
    for (const result of results) {
      const card = document.createElement("section"); card.className = `connection-result ${result.status}`;
      const heading = document.createElement("strong"); heading.textContent = `${result.service} · ${result.status.toUpperCase()}`;
      const address = document.createElement("div"); address.textContent = result.address || "No discovered VM address";
      const detail = document.createElement("p"); detail.textContent = result.detail;
      const timing = document.createElement("small"); timing.textContent = result.duration_ms === undefined ? "Not tested" : `${result.check} · ${result.duration_ms} ms · ${new Date().toLocaleTimeString()}`;
      card.append(heading, address, detail, timing); connectionResults.appendChild(card);
    }
  } catch (error) { connectionResults.textContent = error.message; }
  finally { setConnectionBusy(false); }
});
function closeConnectionSettings() {
  if (connectionDirty && !window.confirm("Discard unsaved settings changes?")) return;
  connectionDialog.close();
}
document.getElementById("connection-close").addEventListener("click", closeConnectionSettings);
connectionDialog.addEventListener("cancel", event => { event.preventDefault(); if (!connectionBusy) closeConnectionSettings(); });
connectionApply.addEventListener("click", async () => { connectionDialog.close(); await enterHost(connectionFilename, false); });

async function enterHost(filename, isActive) {
  if (isActive) {
    designMode = false;
    liveHistory = { undo: [], redo: [] };
    lastPersistedDrawings = [];
    drawingsLoaded = false;
    initialFitDone = false;
    applyDesignModeUI();
    document.getElementById("landing-view").hidden = true;
    document.getElementById("app-view").hidden = false;
    try {
      await loadCanvas();
    } catch (err) {
      setWarningBanner(`Failed to load: ${err}`, { retryable: true });
    }
    return;
  }

  if (!await showConfirm(
    `Switch to "${filename}"? The server restarts against this settings file - any open ` +
    `console/screen connections drop for a moment.`,
    { title: "Switch host", confirmLabel: "Switch", danger: true }
  )) {
    return;
  }

  document.getElementById("landing-host-grid").textContent = `Restarting into "${filename}"…`;
  try {
    await fetch(`/settings-profiles/${encodeURIComponent(filename)}/activate`, { method: "POST" });
  } catch (err) {
    // Expected: the server process re-execs itself shortly after
    // responding (see restart_into_profile), which can drop this
    // request's own connection before a response body ever arrives -
    // that's not a real failure.
  }
  // Poll /health until the restarted process answers again, then just
  // reload the page outright - simplest way to get every piece of
  // state back in sync with the newly active host, rather than trying
  // to patch it all in place.
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    try {
      const resp = await fetch("/health");
      if (resp.ok) { window.location.reload(); return; }
    } catch (err) {
      // still restarting - keep polling
    }
  }
  await showAlert(`Server didn't come back after activating "${filename}" - check it manually.`, { title: "Switch host" });
  await loadLandingHostGrid();
}

async function deleteHostProfile(filename) {
  if (!await showConfirm(`Delete settings file "${filename}"? This doesn't affect a host it's currently active for.`, {
    title: "Delete host", confirmLabel: "Delete", danger: true,
  })) {
    return;
  }
  const resp = await fetch(`/settings-profiles/${encodeURIComponent(filename)}`, { method: "DELETE" });
  if (!resp.ok) {
    await showAlert(`Delete failed: ${(await resp.json()).detail || resp.status}`, { title: "Delete host" });
    return;
  }
  await loadLandingHostGrid();
}

document.getElementById("home-btn").addEventListener("click", () => showLandingView());
document.getElementById("landing-upload-input").addEventListener("change", async (evt) => {
  const input = evt.target;
  const file = input.files[0];
  input.value = "";
  if (!file) return;

  const formData = new FormData();
  formData.append("file", file);
  const resp = await fetch("/settings-profiles", { method: "POST", body: formData });
  if (!resp.ok) {
    await showAlert(`Upload failed: ${(await resp.json()).detail || resp.status}`, { title: "Add host" });
    return;
  }
  await loadLandingHostGrid();
});

document.getElementById("reload-gns3-btn").addEventListener("click", async () => {
  setMenubarBusy("Reloading GNS3 project…");
  try {
    const resp = await fetch("/project/reload-gns3", { method: "POST" });
    if (!resp.ok) {
      setMenubarMessage(`Reload failed: ${(await resp.json()).detail || resp.status}`);
      return;
    }
    await loadCanvas();
    setMenubarMessage("GNS3 project reloaded.");
  } catch (err) {
    setMenubarMessage(`Reload failed: ${err}`);
  }
});

// Compact text report of a /maintenance/verify result: every failed
// check, then the pass count - the full list of passing checks would
// only make the dialog overflow.
function formatVerifyReport(result) {
  const failed = result.results.filter((r) => !r.ok).map((r) => `[FAIL] ${r.description}`);
  const summary = `${result.passed}/${result.total} checks passed`;
  return failed.length ? `${failed.join("\n")}\n\n${summary}` : `All ${result.total} checks passed.`;
}

async function maintenanceRequest(url, options) {
  const resp = await fetch(url, options);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || resp.status);
  return body;
}

document.getElementById("health-check-btn").addEventListener("click", async () => {
  setMenubarBusy("Running health check…");
  try {
    const first = await maintenanceRequest("/maintenance/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fix: false }),
    });
    setMenubarMessage(`Health check: ${first.passed}/${first.total} checks passed.`, { error: first.passed !== first.total });
    if (first.passed === first.total) {
      await showAlert(formatVerifyReport(first), { title: "Health Check" });
      return;
    }
    const wantFix = await showConfirm(
      `${formatVerifyReport(first)}\n\n` +
      `Apply the automatic fixes (start stopped GNS3 nodes, power on stopped ` +
      `ESXi VMs, create missing port groups) and check again? Anything that ` +
      `needs a real redeploy is not touched.`,
      { title: "Health Check", confirmLabel: "Fix" }
    );
    if (!wantFix) return;

    setMenubarBusy("Applying fixes…");
    const second = await maintenanceRequest("/maintenance/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fix: true }),
    });
    const applied = second.actions.length
      ? `Applied:\n${second.actions.map((a) => `- ${a}`).join("\n")}\n\n`
      : "No automatic fix was applicable.\n\n";
    setMenubarMessage(`Health check after fixes: ${second.passed}/${second.total} checks passed.`, { error: second.passed !== second.total });
    await showAlert(applied + formatVerifyReport(second), { title: "Health Check" });
    await loadCanvas();

    // Never part of the automatic fixes: re-creating the Cloud nodes can briefly cut every
    // link between the GNS3 VM and the ESXi VMs, so it is its own, explicit confirmation.
    if (!second.results.some((r) => !r.ok && /VLAN subinterfaces/i.test(r.description))) return;
    const wantSubinterfaces = await showConfirm(
      "The GNS3 VM is missing VLAN subinterfaces (they are lost when it reboots), so links " +
      "between GNS3 nodes and ESXi VMs carry no traffic.\n\n" +
      "Re-create them? This restarts the Cloud nodes and briefly interrupts those links. " +
      "Do it while nothing important is running.",
      { title: "Re-create VLAN subinterfaces", confirmLabel: "Re-create", danger: true }
    );
    if (!wantSubinterfaces) return;

    setMenubarBusy("Re-creating VLAN subinterfaces…");
    const third = await maintenanceRequest("/maintenance/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fix: false, fix_vlan_subinterfaces: true }),
    });
    const repaired = third.actions.length
      ? `Applied:\n${third.actions.map((a) => `- ${a}`).join("\n")}\n\n`
      : "Nothing to re-create.\n\n";
    setMenubarMessage(`Health check after re-creating subinterfaces: ${third.passed}/${third.total} checks passed.`, { error: third.passed !== third.total });
    await showAlert(repaired + formatVerifyReport(third), { title: "Health Check" });
    await loadCanvas();
  } catch (err) {
    setMenubarMessage(`Health check failed: ${err.message || err}`);
  }
});

// Text report of a finished connectivity test: one line per segment with the pairs that lost
// packets spelled out, then what was skipped and what went wrong.
function formatConnectivityReport(result) {
  const lines = [];
  for (const segment of result.segments) {
    const hosts = Object.entries(segment.hosts).map(([name, address]) => `${name} (${address})`);
    lines.push(`Segment ${segment.segment + 1}: ${hosts.join(", ")}`);
    const pairs = result.pairs.filter((p) => p.source in segment.hosts);
    const received = pairs.reduce((sum, p) => sum + p.received, 0);
    const sent = pairs.reduce((sum, p) => sum + p.sent, 0);
    lines.push(`  ${received}/${sent} pings answered`);
    const rtts = pairs.map((p) => p.rtt_avg_ms).filter((v) => v !== null);
    if (rtts.length) {
      lines.push(`  average round trip ${(rtts.reduce((a, b) => a + b, 0) / rtts.length).toFixed(1)} ms`);
    }
    for (const p of pairs.filter((p) => p.received < p.sent)) {
      lines.push(`  [FAIL] ${p.source} -> ${p.target}: ${p.loss_percent}% lost`);
    }
  }
  for (const [name, reason] of Object.entries(result.skipped)) lines.push(`Skipped ${name}: ${reason}`);
  for (const [name, reason] of Object.entries(result.problems)) lines.push(`[PROBLEM] ${name}: ${reason}`);
  if (result.error) lines.push(`[ERROR] ${result.error}`);
  lines.push("", result.all_ok ? "All pings answered." : "Not every ping was answered.");
  return lines.join("\n");
}

document.getElementById("connectivity-test-btn").addEventListener("click", async () => {
  const confirmed = await showConfirm(
    "The VMs that share a switch get a temporary address (198.18.x.x) each, every one pings every " +
    "other, and the addresses are removed again - also when something fails on the way. VMs " +
    "behind a router are not tested against each other.\n\nThis takes a few minutes. Start it?",
    { title: "Connectivity Test", confirmLabel: "Start" }
  );
  if (!confirmed) return;
  setMenubarBusy("Starting connectivity test…");
  try {
    const { job_id: jobId } = await maintenanceRequest("/maintenance/connectivity-test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 3000));
      const status = await maintenanceRequest(`/maintenance/connectivity-test/${jobId}`);
      if (status.status === "running") {
        setMenubarBusy(`Connectivity test: ${status.phase || "working"}… (${Math.round(status.elapsed_seconds)} s)`);
        continue;
      }
      setMenubarMessage(
        status.all_ok ? "Connectivity test: every ping answered." : "Connectivity test: not every ping was answered.",
        { error: !status.all_ok }
      );
      await showAlert(formatConnectivityReport(status), { title: "Connectivity Test" });
      return;
    }
  } catch (err) {
    setMenubarMessage(`Connectivity test failed: ${err.message || err}`);
  }
});

document.getElementById("find-orphans-btn").addEventListener("click", async () => {
  setMenubarBusy("Looking for orphans…");
  try {
    const { orphans } = await maintenanceRequest("/maintenance/orphans");
    if (!orphans.length) {
      setMenubarMessage("No orphans found.");
      await showAlert("No orphans found - every live node and tool-created VM is in the topology file.", { title: "Find Orphans" });
      return;
    }
    setMenubarMessage(`${orphans.length} orphan(s) found.`);
    const confirmed = await showConfirm(
      `${orphans.map((o) => `[${o.type}] ${o.name}`).join("\n")}\n\n` +
      `These exist live but no node in the topology file describes them. ` +
      `Permanently delete ${orphans.length === 1 ? "it" : "all of them"}? This cannot be undone.`,
      { title: `${orphans.length} Orphan(s) Found`, confirmLabel: "Delete", danger: true }
    );
    if (!confirmed) return;

    setMenubarBusy("Deleting orphans…");
    const { deleted } = await maintenanceRequest("/maintenance/orphans/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ orphans }),
    });
    setMenubarMessage(`Deleted ${deleted} orphan(s).`);
    await loadCanvas();
  } catch (err) {
    setMenubarMessage(`Find orphans failed: ${err.message || err}`);
  }
});

function openTerminalWebSocket(session, wsUrl) {
  const termHost = document.createElement("div");
  termHost.className = "session-terminal";
  session.bodyEl.innerHTML = "";
  session.bodyEl.appendChild(termHost);

  const term = new Terminal({ convertEol: true, fontSize: 13, theme: { background: "#1a1d1f" } });
  term.open(termHost);
  session.term = term;

  const ws = new WebSocket(wsUrl);
  ws.binaryType = "arraybuffer";
  session.ws = ws;

  ws.onopen = () => setSessionStatus(session, "connected", "status-ok");
  ws.onmessage = (event) => { term.write(new Uint8Array(event.data)); };
  ws.onclose = (event) => {
    setSessionStatus(session, event.reason ? `closed: ${event.reason}` : "closed", "status-bad");
  };
  ws.onerror = () => setSessionStatus(session, "error", "status-bad");

  term.onData((data) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(new TextEncoder().encode(data));
  });
}

// The serial line of an ESXi VM, in the same terminal panel as a GNS3 console. Opening it again after it closed
// (VM was off, port busy) reconnects instead of just switching to the dead tab.
function openSerialConsole(nodeName) {
  const id = sessionIdFor(nodeName, "serial");
  const session = ensureSessionPanel(id, nodeName, `${nodeName} (serial)`);
  switchToSession(id);
  const wasNew = !session.kind;
  surfaceSession(session, wasNew);
  if (session.kind && session.ws && session.ws.readyState !== WebSocket.CLOSED) return;
  session.kind = "terminal";
  setSessionStatus(session, "connecting…");

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  openTerminalWebSocket(session, `${proto}//${location.host}/ws/serial/${encodeURIComponent(nodeName)}`);
  session.ws.onopen = () => setSessionStatus(session, "connected - press Enter if the screen stays empty", "status-ok");
}

function openConsole(nodeName) {
  const id = sessionIdFor(nodeName, "console");
  const session = ensureSessionPanel(id, nodeName, nodeName);
  switchToSession(id);
  surfaceSession(session, !session.kind);
  if (session.kind) return; // already open or opening - just switched to its tab
  session.kind = "terminal";
  setSessionStatus(session, "connecting…");

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  openTerminalWebSocket(session, `${proto}//${location.host}/ws/console/${encodeURIComponent(nodeName)}`);
}

async function openVmScreen(nodeName, opts) {
  const isReconnect = !!(opts && opts.isReconnect);
  const id = sessionIdFor(nodeName, "screen");
  let session;
  if (isReconnect) {
    session = sessions.get(id);
    if (!session || !session.screenWanted) return; // tab closed/gave up while this was pending
  } else {
    session = ensureSessionPanel(id, nodeName, `${nodeName} (screen)`);
    switchToSession(id);
    surfaceSession(session, !session.kind);
    if (session.kind) return;
    session.kind = "screen";
    session.screenWanted = true;
    session.reconnectAttempts = 0;
  }

  setSessionStatus(session, isReconnect ? "reconnecting…" : "connecting…");

  const screenHost = document.createElement("div");
  screenHost.className = "session-screen";
  session.bodyEl.innerHTML = "";
  session.bodyEl.appendChild(screenHost);

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws/vm-screen/${encodeURIComponent(nodeName)}`;

  let RFB;
  try {
    ({ default: RFB } = await import("/vendor/novnc/core/rfb.js"));
  } catch (err) {
    setSessionStatus(session, `couldn't load screen viewer: ${err}`, "status-bad");
    return;
  }
  if (!session.screenWanted) return; // closed while the module was loading

  const thisRfb = new RFB(screenHost, url, { wsProtocols: ["binary"] });
  session.rfb = thisRfb;
  thisRfb.scaleViewport = true;
  thisRfb.resizeSession = false;
  thisRfb.showDotCursor = true;

  thisRfb.addEventListener("connect", () => {
    session.reconnectAttempts = 0;
    setSessionStatus(session, "connected", "status-ok");
  });
  thisRfb.addEventListener("disconnect", () => {
    // session.rfb !== thisRfb: a newer attempt has already superseded
    // this one - don't double-reconnect or clobber its state.
    if (!session.screenWanted || session.rfb !== thisRfb) return;

    session.reconnectAttempts += 1;
    if (session.reconnectAttempts > VM_SCREEN_MAX_CONSECUTIVE_FAILURES) {
      setSessionStatus(session, "screen keeps disconnecting - giving up for now, click Screen to retry", "status-bad");
      session.screenWanted = false;
      return;
    }
    setSessionStatus(session, "reconnecting…");
    setTimeout(() => openVmScreen(nodeName, { isReconnect: true }), 300);
  });
  thisRfb.addEventListener("credentialsrequired", () => {
    setSessionStatus(session, "server requires credentials (not supported by this viewer)", "status-bad");
  });
  thisRfb.addEventListener("securityfailure", (evt) => {
    const detail = evt.detail || {};
    setSessionStatus(session, `security negotiation failed: ${detail.reason || detail.status || "unknown"}`, "status-bad");
  });
}

document.getElementById("tab-console").addEventListener("click", () => showTab("console"));
document.getElementById("tab-push").addEventListener("click", () => showTab("push"));
document.getElementById("tab-logs").addEventListener("click", () => showTab("logs"));

// GNS3 Desktop-style menu bar: click a menu-btn to toggle its dropdown,
// click anywhere else (including a menu-item, which bubbles here after
// running its own action) to close it - same "click outside closes it"
// convention as the rest of this app's own popovers.
function closeAllMenus() {
  document.querySelectorAll(".menu-dropdown").forEach((m) => { m.hidden = true; });
  document.querySelectorAll(".menu-btn").forEach((b) => b.classList.remove("open"));
}
document.querySelectorAll(".menu-btn[data-menu]").forEach((btn) => {
  btn.addEventListener("click", (evt) => {
    evt.stopPropagation();
    const menu = document.getElementById(`menu-${btn.dataset.menu}`);
    const wasOpen = !menu.hidden;
    closeAllMenus();
    if (!wasOpen) {
      menu.hidden = false;
      btn.classList.add("open");
    }
  });
});
document.addEventListener("click", closeAllMenus);

// The node-actions popup closes on a click anywhere else - except a
// click that itself just opened/repositioned it (a node icon or a
// Topology Summary row, both already handled by selectNode) or a
// click on one of its own buttons (Start/Stop/etc. stay open
// afterward, same as the old docked panel always did, so their
// result is visible without re-selecting the node).
function deselectNode() {
  selectedName = null;
  selectedNames = new Set();
  updateStatusBar();
  hideMultiActions();
  document.getElementById("node-actions").hidden = true;
  document.querySelectorAll(".node-shape.selected").forEach((g) => g.classList.remove("selected"));
  document.querySelectorAll(".summary-row.selected").forEach((r) => r.classList.remove("selected"));
}
let ignoreNextDocumentClick = false; // set when a rubber-band drag ends: its trailing click must not clear the new selection
document.addEventListener("click", (evt) => {
  if (ignoreNextDocumentClick) { ignoreNextDocumentClick = false; return; }
  const popup = document.getElementById("node-actions");
  const multi = document.getElementById("multi-actions");
  if (popup.hidden && multi.hidden) return;
  if (popup.contains(evt.target) || multi.contains(evt.target)) return;
  if (evt.target.closest(".node-shape") || evt.target.closest(".summary-row")) return;
  deselectNode();
});

// Settings has live fields (a number input, checkboxes) rather than
// one-shot commands - unlike File/View/Info's own menu-items, clicking
// inside it shouldn't immediately close it (the generic
// document-click-closes-everything listener above would otherwise
// close it after every single checkbox toggle).
document.getElementById("menu-settings").addEventListener("click", (evt) => evt.stopPropagation());

document.getElementById("settings-zoom-input").addEventListener("change", (evt) => {
  setZoomPercent(parseFloat(evt.target.value));
});

document.getElementById("settings-show-grid").addEventListener("change", (evt) => {
  document.querySelector(".grid-bg").style.display = evt.target.checked ? "" : "none";
});

document.getElementById("settings-snap-grid").addEventListener("change", (evt) => {
  snapToGridEnabled = evt.target.checked;
});

document.getElementById("reset-labels-btn").addEventListener("click", async () => {
  if (!Object.keys(labelOffsets).length) return;
  const changes = Object.fromEntries(Object.keys(labelOffsets).map((key) => [key, null]));
  labelOffsets = {};
  await saveLabelOffsets(changes);
  renderCanvas();
});

document.getElementById("settings-port-labels").addEventListener("change", (evt) => {
  showPortLabels = evt.target.checked;
  renderCanvas();
});
