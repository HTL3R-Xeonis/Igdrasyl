// [consoles-and-push.js] Console windows, the logs tab, the config push tab and the project's saved device configs. Loaded in the order listed in index.html; all scripts share one global scope.
const consoleWindowToggle = document.getElementById("settings-console-window");
consoleWindowToggle.checked = openConsolesInWindow;
consoleWindowToggle.addEventListener("change", (evt) => {
  openConsolesInWindow = evt.target.checked;
  try {
    localStorage.setItem("ginnungagap.openConsolesInWindow", openConsolesInWindow ? "1" : "0");
  } catch (err) {
    // not remembered, still in effect until the page is reloaded
  }
});

document.getElementById("settings-disruption-warning").addEventListener("change", (evt) => {
  showDisruptionWarnings = evt.target.checked;
});

document.querySelectorAll("#menu-draw .menu-item[data-draw]").forEach((btn) => {
  btn.addEventListener("click", () => setDrawMode(btn.dataset.draw));
});

// "Image" doesn't fit the other tools' click-drag-to-size flow (there's
// nothing to draw until a file is picked) - picking a file places it
// immediately, centered in the current view and already selected
// (resize handles showing) so it can be dragged/resized right away,
// same as a freshly-drawn rect/ellipse would be.
const DRAW_IMAGE_MAX_INITIAL_SIZE = 320;
document.getElementById("draw-image-btn").addEventListener("click", () => {
  setDrawMode(null);
  document.getElementById("draw-image-input").click();
});
document.getElementById("draw-image-input").addEventListener("change", async (evt) => {
  const input = evt.target;
  const file = input.files[0];
  input.value = "";
  if (!file) return;

  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  }).catch((err) => {
    showAlert(`Couldn't read "${file.name}": ${err}`, { title: "Add image" });
    return null;
  });
  if (!dataUrl) return;

  // Natural size for a sensible default placement - works for GIF/PNG/
  // JPEG directly; for SVG it depends on the file declaring its own
  // width/height or viewBox, and falls back to the same default box
  // rect/ellipse use (via naturalWidth/Height coming back 0) otherwise.
  const natural = await new Promise((resolve) => {
    const probe = new Image();
    probe.onload = () => resolve({ w: probe.naturalWidth, h: probe.naturalHeight });
    probe.onerror = () => resolve({ w: 0, h: 0 });
    probe.src = dataUrl;
  });

  let w = natural.w || 200;
  let h = natural.h || 150;
  if (w > DRAW_IMAGE_MAX_INITIAL_SIZE || h > DRAW_IMAGE_MAX_INITIAL_SIZE) {
    const scale = DRAW_IMAGE_MAX_INITIAL_SIZE / Math.max(w, h);
    w *= scale;
    h *= scale;
  }

  const x1 = viewBoxState.x + viewBoxState.w / 2 - w / 2;
  const y1 = viewBoxState.y + viewBoxState.h / 2 - h / 2;
  const d = { id: ++drawIdCounter, type: "image", x1, y1, x2: x1 + w, y2: y1 + h, href: dataUrl };
  drawings.push(d);
  selectedDrawingId = d.id;
  renderDrawings();
  persistDrawings();
});

document.getElementById("draw-clear-btn").addEventListener("click", async () => {
  if (!drawings.length) return;
  if (await showConfirm("Clear every drawing from the canvas?", { danger: true, confirmLabel: "Clear" })) {
    drawings = [];
    selectedDrawingId = null;
    renderDrawings();
    persistDrawings();
  }
});

function showTab(name, { loadConfigs = true } = {}) {
  document.getElementById("right-panel").hidden = name !== "console";
  document.getElementById("push-area").hidden = name !== "push";
  document.getElementById("logs-area").hidden = name !== "logs";
  document.getElementById("tab-console").classList.toggle("active", name === "console");
  document.getElementById("tab-push").classList.toggle("active", name === "push");
  document.getElementById("tab-logs").classList.toggle("active", name === "logs");
  if (name === "push" && !designMode) {
    if (document.getElementById("push-history").hidden) {
      if (loadConfigs) loadProjectConfigs(true);
    }
    else loadPushHistory();
  }
  if (name === "logs") startLogsAutoRefresh(); else stopLogsAutoRefresh();
}

const LOGS_AUTO_REFRESH_INTERVAL_MS = 3000;
let logsAutoRefreshTimer = null;

async function loadLogs() {
  const output = document.getElementById("logs-output");
  let lines;
  try {
    lines = await fetch("/logs").then((r) => r.json());
  } catch (err) {
    output.innerHTML = "";
    const empty = document.createElement("div");
    empty.id = "logs-empty";
    empty.textContent = `Failed to load logs: ${err}`;
    output.appendChild(empty);
    return;
  }

  // Auto-scroll only if already at (or near) the bottom - otherwise a
  // periodic auto-refresh would keep yanking the view back down while
  // someone's trying to read an earlier entry.
  const wasAtBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 20;

  output.innerHTML = "";
  if (!lines.length) {
    const empty = document.createElement("div");
    empty.id = "logs-empty";
    empty.textContent = "No log entries yet.";
    output.appendChild(empty);
    return;
  }
  for (const entry of lines) {
    output.appendChild(buildLogLine(entry));
  }
  if (wasAtBottom) output.scrollTop = output.scrollHeight;
}

function buildLogLine(entry) {
  const row = document.createElement("div");
  row.className = "log-line";
  if (!entry.time) {
    // A line that didn't match the expected format (e.g. a traceback's
    // continuation lines) - shown verbatim, dimmed, no columns.
    row.classList.add("log-line-raw");
    row.textContent = entry.message;
    return row;
  }
  const meta = document.createElement("div");
  meta.className = "log-line-meta";
  const time = document.createElement("span");
  time.className = "log-line-time";
  time.textContent = entry.time;
  const level = document.createElement("span");
  level.className = `log-line-level log-level-${entry.level}`;
  level.textContent = entry.level;
  const location = document.createElement("span");
  location.className = "log-line-location";
  location.textContent = entry.location;
  meta.append(time, level, location);

  const message = document.createElement("div");
  message.className = `log-line-message log-level-${entry.level}`;
  message.textContent = entry.message;

  row.append(meta, message);
  return row;
}

function startLogsAutoRefresh() {
  loadLogs();
  stopLogsAutoRefresh();
  if (document.getElementById("logs-auto-refresh").checked) {
    logsAutoRefreshTimer = setInterval(loadLogs, LOGS_AUTO_REFRESH_INTERVAL_MS);
  }
}
function stopLogsAutoRefresh() {
  if (logsAutoRefreshTimer) {
    clearInterval(logsAutoRefreshTimer);
    logsAutoRefreshTimer = null;
  }
}
document.getElementById("logs-refresh-btn").addEventListener("click", () => loadLogs());
document.getElementById("logs-clear-btn").addEventListener("click", async () => {
  if (!await showConfirm(
    "Delete all log entries permanently? This can't be undone.",
    { title: "Clear logs", confirmLabel: "Clear", danger: true }
  )) return;
  const resp = await fetch("/logs", { method: "DELETE" });
  if (!resp.ok) {
    await showAlert(`Clearing the logs failed: ${(await resp.json().catch(() => ({}))).detail || resp.status}`, { title: "Clear logs" });
    return;
  }
  await loadLogs();
});
document.getElementById("logs-auto-refresh").addEventListener("change", () => startLogsAutoRefresh());

function renderPushResults(deviceResults) {
  const resultsEl = document.getElementById("push-results");
  resultsEl.replaceChildren();
  for (const result of deviceResults) {
    const box = document.createElement("div");
    box.className = "device-result";
    const header = document.createElement("div");
    header.className = `device-result-header ${result.ok ? "ok" : "fail"}`;
    const title = document.createElement("span");
    title.textContent = `${result.device} `;
    const tag = document.createElement("span");
    tag.className = "env-tag";
    tag.textContent = result.transport;
    title.appendChild(tag);
    const state = document.createElement("span");
    state.textContent = result.ok ? "ok" : "failed";
    header.append(title, state);
    box.appendChild(header);

    if (result.error) {
      const err = document.createElement("div");
      err.style.padding = "6px 10px";
      err.style.color = "var(--bad)";
      err.textContent = result.error;
      box.appendChild(err);
    }
    for (const c of result.commands || []) {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;gap:8px;padding:3px 10px;align-items:baseline";
      const mark = document.createElement("span");
      mark.className = c.ok ? "cmd-ok" : "cmd-fail";
      mark.textContent = c.ok ? "✓" : "✗";
      const cmd = document.createElement("code");
      cmd.textContent = c.command;
      row.append(mark, cmd);
      box.appendChild(row);
      const output = (c.output || "").trim();
      if (output) {
        // Real command output (guest operations capture it too): shown in full,
        // folded open for a failure, always as text.
        const details = document.createElement("details");
        details.open = !c.ok;
        const summary = document.createElement("summary");
        summary.textContent = `output (${output.split("\n").length} line${output.includes("\n") ? "s" : ""})`;
        const pre = document.createElement("pre");
        pre.className = "cmd-output";
        pre.textContent = output;
        details.append(summary, pre);
        box.appendChild(details);
      }
    }
    resultsEl.appendChild(box);
  }
}

// --- Device configs saved with the project (library project of the active host) ---

function setPushNote(text, isError = false) {
  const note = document.getElementById("push-project-note");
  note.textContent = text;
  note.classList.toggle("push-note-error", isError);
}

async function loadProjectConfigs(silent = false) {
  const input = document.getElementById("push-input");
  const previous = input.value;
  try {
    const resp = await fetch("/project/configs");
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.detail || resp.status);
    if (input.value !== previous) return;
    const count = Object.keys(body.devices).length;
    if (count) input.value = JSON.stringify(body.devices, null, 2);
    document.getElementById("push-auto-checkbox").checked = !!body.auto_push;
    setPushNote(count
      ? `Loaded the configs of ${count} device${count === 1 ? "" : "s"} saved with this project.`
      : "This project has no saved configs yet - write them above and press Save to project.");
  } catch (err) {
    if (!silent) setPushNote(`Couldn't load: ${err.message || err}`, true);
  }
}

document.getElementById("push-load-project-btn").addEventListener("click", () => loadProjectConfigs());
document.getElementById("push-save-project-btn").addEventListener("click", async () => {
  let devices;
  try {
    devices = JSON.parse(document.getElementById("push-input").value);
  } catch (err) {
    setPushNote(`Invalid JSON: ${err}`, true);
    return;
  }
  try {
    const resp = await fetch("/project/configs", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ devices, auto_push: document.getElementById("push-auto-checkbox").checked }),
    });
    const body = await resp.json();
    if (!resp.ok) throw new Error(typeof body.detail === "string" ? body.detail : "invalid configs");
    const count = Object.keys(body.devices).length;
    setPushNote(`Saved with the project: ${count} device${count === 1 ? "" : "s"}${body.auto_push ? ", pushed automatically after deploy and resume" : ""}.`);
  } catch (err) {
    setPushNote(`Couldn't save: ${err.message || err}`, true);
  }
});

function mergePushSnippet(devices, device, commands) {
  if (!devices || typeof devices !== "object" || Array.isArray(devices)) {
    throw new Error("Expected a JSON object mapping device names to commands.");
  }
  const current = Object.hasOwn(devices, device) ? devices[device] : [];
  const list = Array.isArray(current) ? current : current?.commands;
  if (!Array.isArray(list) || list.some(command => typeof command !== "string")) {
    throw new Error(`Invalid command list for ${device}.`);
  }
  if (list.length + commands.length > 10000) {
    throw new Error(`${device} would exceed 10,000 commands.`);
  }
  const merged = [...list, ...commands];
  return { ...devices, [device]: Array.isArray(current) ? merged : { ...current, commands: merged } };
}

function pushSnippetTargets(input) {
  let existing = [];
  try {
    const devices = JSON.parse(input);
    if (devices && typeof devices === "object" && !Array.isArray(devices)) {
      existing = Object.keys(devices);
    }
  } catch {} // Let the operator fix the JSON before inserting anything.
  return [...new Set([...Object.keys(nodesData), ...existing])].sort();
}

const pushSnippetsToggle = document.getElementById("push-snippets-toggle");
const pushSnippetsPanel = document.getElementById("push-snippets");
const pushSnippetName = document.getElementById("push-snippet-name");
const pushSnippetDevice = document.getElementById("push-snippet-device");
const pushSnippetInsert = document.getElementById("push-snippet-insert");
const aiSnippetPanel = document.getElementById("push-snippet-ai");
const aiSnippetTopic = document.getElementById("push-snippet-ai-topic");
const aiSnippetDevice = document.getElementById("push-snippet-ai-device");
const aiSnippetPrompt = document.getElementById("push-snippet-ai-prompt");
const aiSnippetGenerate = document.getElementById("push-snippet-ai-generate");
const aiSnippetReview = document.getElementById("push-snippet-ai-review");
const aiSnippetStatus = document.getElementById("push-snippet-ai-status");
const aiSnippetPush = document.getElementById("push-snippet-ai-push");
let aiSnippetDraft = null;
let aiSnippetTargetsRequest = 0;

async function refreshSnippetNames() {
  const snippets = await settingsRequest("/config-snippets");
  pushSnippetName.replaceChildren(...(snippets.length
    ? snippets.map(snippet => new Option(snippet.label, snippet.name))
    : [new Option("No saved snippets", "")]));
  pushSnippetInsert.disabled = !pushSnippetName.value || !pushSnippetDevice.value;
}

async function refreshAiSnippetTopics() {
  if (aiSnippetTopic.disabled) return;
  try {
    const topics = await settingsRequest("/config-snippets/ai/topics");
    const selected = aiSnippetTopic.value;
    aiSnippetTopic.replaceChildren(...topics.map(topic => new Option(topic.label, topic.id)));
    if (topics.some(topic => topic.id === selected)) aiSnippetTopic.value = selected;
    if (selected !== aiSnippetTopic.value) clearAiSnippetDraft();
    aiSnippetPanel.hidden = !topics.length;
    if (topics.length) await refreshAiSnippetTargets();
  } catch {
    aiSnippetPanel.hidden = true;
  }
}

function clearAiSnippetDraft() {
  aiSnippetDraft = null;
  aiSnippetReview.hidden = true;
  aiSnippetStatus.textContent = "";
}

async function refreshAiSnippetTargets() {
  clearAiSnippetDraft();
  const request = ++aiSnippetTargetsRequest;
  aiSnippetGenerate.disabled = true;
  aiSnippetDevice.replaceChildren(new Option("Loading devices…", ""));
  try {
    const devices = await settingsRequest(`/config-snippets/ai/targets?topic=${encodeURIComponent(aiSnippetTopic.value)}`);
    if (request !== aiSnippetTargetsRequest) return;
    aiSnippetDevice.replaceChildren(new Option(devices.length ? "Choose a device…" : "No compatible devices", ""));
    for (const name of devices) aiSnippetDevice.add(new Option(name, name));
  } catch (error) {
    if (request !== aiSnippetTargetsRequest) return;
    aiSnippetDevice.replaceChildren(new Option("Could not load devices", ""));
    aiSnippetStatus.textContent = error.message;
  }
}

function setAiSnippetBusy(busy) {
  aiSnippetPush.disabled = busy;
  aiSnippetGenerate.disabled = busy || !aiSnippetDevice.value;
  aiSnippetTopic.disabled = busy;
  aiSnippetDevice.disabled = busy;
  aiSnippetPrompt.disabled = busy;
}

aiSnippetTopic.addEventListener("change", refreshAiSnippetTargets);
aiSnippetDevice.addEventListener("change", () => {
  clearAiSnippetDraft();
  aiSnippetGenerate.disabled = !aiSnippetDevice.value;
});
aiSnippetPrompt.addEventListener("input", clearAiSnippetDraft);
document.getElementById("push-snippet-ai-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!aiSnippetTopic.value || !aiSnippetDevice.value || aiSnippetGenerate.disabled) return;
  clearAiSnippetDraft();
  const topic = aiSnippetTopic.value;
  const device = aiSnippetDevice.value;
  const prompt = aiSnippetPrompt.value.trim();
  if (!prompt) {
    aiSnippetStatus.textContent = "Enter a prompt.";
    return;
  }
  setAiSnippetBusy(true);
  aiSnippetStatus.textContent = "Generating draft…";
  try {
    aiSnippetDraft = await settingsRequest("/config-snippets/ai/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic, device, prompt }),
    });
    document.getElementById("push-snippet-ai-commands").textContent = aiSnippetDraft.commands.join("\n");
    aiSnippetReview.hidden = false;
    aiSnippetStatus.textContent = `${aiSnippetDraft.device} · ${aiSnippetDraft.label}`;
  } catch (error) {
    aiSnippetStatus.textContent = error.message;
  } finally {
    setAiSnippetBusy(false);
  }
});

aiSnippetPush.addEventListener("click", async () => {
  const manualPush = document.getElementById("push-btn");
  if (!aiSnippetDraft || aiSnippetPush.disabled || manualPush.disabled) return;
  const draft = aiSnippetDraft;
  setAiSnippetBusy(true);
  manualPush.disabled = true;
  try {
    if (!await showConfirm(
      `Push these commands to ${draft.device}?\n\n${draft.commands.join("\n")}`,
      { title: "Push AI configuration", confirmLabel: "Push", danger: true }
    )) return;
    const { job_id } = await settingsRequest("/config-snippets/ai/push", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft_id: draft.draft_id }),
    });
    clearAiSnippetDraft();
    document.getElementById("push-results").textContent = `Pushing to ${draft.device}…`;
    await followPushJob(job_id);
  } catch (error) {
    aiSnippetStatus.textContent = error.message;
  } finally {
    manualPush.disabled = false;
    setAiSnippetBusy(false);
  }
});

pushSnippetsToggle.addEventListener("click", async () => {
  const opening = pushSnippetsPanel.hidden;
  pushSnippetsPanel.hidden = !opening;
  pushSnippetsToggle.setAttribute("aria-expanded", String(opening));
  if (!opening) return;
  refreshAiSnippetTopics();
  pushSnippetsToggle.disabled = true;
  pushSnippetInsert.disabled = true;
  pushSnippetName.replaceChildren(new Option("Loading snippets…", ""));
  try {
    const targets = pushSnippetTargets(document.getElementById("push-input").value);
    pushSnippetDevice.replaceChildren(...(targets.length
      ? [new Option("Choose a device…", ""), ...targets.map(name => new Option(name, name))]
      : [new Option("No target devices", "")]));
    await refreshSnippetNames();
  } catch (err) {
    pushSnippetName.replaceChildren(new Option("Could not load snippets", ""));
    setPushNote(`Couldn't load snippets: ${err.message}`, true);
  } finally {
    pushSnippetsToggle.disabled = false;
  }
});

pushSnippetDevice.addEventListener("change", () => {
  pushSnippetInsert.disabled = !pushSnippetName.value || !pushSnippetDevice.value;
});
pushSnippetInsert.addEventListener("click", async () => {
  const device = pushSnippetDevice.value;
  const name = pushSnippetName.value;
  if (!device || !name) return;
  pushSnippetInsert.disabled = true;
  try {
    const response = await fetch(`/config-snippets/${encodeURIComponent(name)}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const snippet = await response.json();
    const input = document.getElementById("push-input");
    const merged = mergePushSnippet(JSON.parse(input.value), device, snippet.commands);
    input.value = JSON.stringify(merged, null, 2);
    setPushNote(`Inserted ${snippet.label} for ${device}. Review the commands before pushing.`);
  } catch (err) {
    setPushNote(`Couldn't insert snippet: ${err.message}`, true);
  } finally {
    pushSnippetInsert.disabled = !pushSnippetName.value || !pushSnippetDevice.value;
  }
});

async function loadPushHistory() {
  const note = document.getElementById("push-history-note");
  const table = document.getElementById("push-history-table");
  const rows = table.tBodies[0];
  const refresh = document.getElementById("push-history-refresh");
  if (refresh.disabled) return;
  refresh.disabled = true;
  try {
    const response = await fetch("/config-push/audit");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const entries = await response.json();
    rows.replaceChildren();
    for (const entry of entries) {
      const row = rows.insertRow();
      const run = row.insertCell();
      run.textContent = new Date(entry.timestamp).toLocaleString();
      const source = document.createElement("div");
      source.textContent = entry.note || "";
      run.appendChild(source);
      row.insertCell().textContent = { ok: "Succeeded", failed: "Failed", interrupted: "Interrupted" }[entry.status] || entry.status;
      const devices = row.insertCell();
      for (const device of entry.devices) {
        const item = document.createElement("div");
        item.textContent = `${device.device}: ${device.executed}/${device.total_commands} · ${device.ok ? "OK" : "Failed"}`;
        devices.appendChild(item);
      }
      if (!entry.devices.length) devices.textContent = "—";
    }
    table.hidden = entries.length === 0;
    note.textContent = entries.length ? "" : "No config pushes recorded yet.";
  } catch (err) {
    table.hidden = true;
    note.textContent = `Couldn't load push history: ${err.message}`;
  } finally {
    refresh.disabled = false;
  }
}

function showPushHistory(show) {
  document.getElementById("push-editor").hidden = show;
  document.getElementById("push-history").hidden = !show;
  for (const [id, active] of [["push-editor-tab", !show], ["push-history-tab", show]]) {
    const button = document.getElementById(id);
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  if (show) loadPushHistory();
}

document.getElementById("push-editor-tab").addEventListener("click", () => showPushHistory(false));
document.getElementById("push-history-tab").addEventListener("click", () => showPushHistory(true));
document.getElementById("push-history-refresh").addEventListener("click", loadPushHistory);

// Follows a config push job (POST /config-push/jobs) until it ends: per-device progress
// while it runs, the full per-device results afterwards.
async function followPushJob(jobId) {
  const resultsEl = document.getElementById("push-results");
  for (;;) {
    let body;
    try {
      const resp = await fetch(`/config-push/jobs/${jobId}`);
      body = await resp.json();
      if (!resp.ok) throw new Error(body.detail || resp.status);
    } catch (err) {
      resultsEl.textContent = `Lost the push job: ${err.message || err}`;
      return null;
    }
    if (body.status === "running") {
      renderPushProgress(body);
      await new Promise((resolve) => setTimeout(resolve, 1000));
      continue;
    }
    if (!document.getElementById("push-area").hidden && !document.getElementById("push-history").hidden) {
      loadPushHistory();
    }
    if (body.status === "error") {
      resultsEl.textContent = `The push failed: ${body.error}`;
      return null;
    }
    renderPushResults(body.results);
    return body.results;
  }
}

// The line shown while a push waits for devices that are still booting (job.waiting_for,
// retried by the server on its own); empty when nothing is waited for.
function pushWaitingText(names) {
  if (!Array.isArray(names) || names.length === 0) return "";
  return `Waiting for: ${names.join(", ")} (still booting)`;
}

function renderPushProgress(job) {
  const resultsEl = document.getElementById("push-results");
  resultsEl.replaceChildren();
  const heading = document.createElement("div");
  heading.style.cssText = "margin:6px 0;color:var(--dim);font-size:12px";
  heading.textContent = `Pushing… ${job.done_commands.toLocaleString()} of ${job.total_commands.toLocaleString()} commands, ${Math.round(job.elapsed_seconds)} s`;
  resultsEl.appendChild(heading);
  const waitingText = pushWaitingText(job.waiting_for);
  if (waitingText) {
    const waiting = document.createElement("div");
    waiting.style.cssText = "margin:6px 0;color:var(--dim);font-size:12px";
    waiting.textContent = waitingText;
    resultsEl.appendChild(waiting);
  }
  for (const device of job.devices) {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px";
    const name = document.createElement("span");
    name.style.minWidth = "80px";
    name.textContent = device.device;
    const track = document.createElement("span");
    track.className = "menubar-progress-track";
    track.style.flex = "1";
    const fill = document.createElement("span");
    fill.className = "menubar-progress-fill";
    fill.style.width = `${device.total ? Math.round((device.done / device.total) * 100) : 0}%`;
    track.appendChild(fill);
    const count = document.createElement("span");
    count.style.color = "var(--dim)";
    count.textContent = `${device.done.toLocaleString()} / ${device.total.toLocaleString()}`;
    row.append(name, track, count);
    resultsEl.appendChild(row);
  }
}

document.getElementById("push-btn").addEventListener("click", async () => {
  const resultsEl = document.getElementById("push-results");
  let devices;
  try {
    devices = JSON.parse(document.getElementById("push-input").value);
  } catch (err) {
    resultsEl.textContent = `Invalid JSON: ${err}`;
    return;
  }
  const button = document.getElementById("push-btn");
  button.disabled = true;
  resultsEl.textContent = "Starting…";
  try {
    const resp = await fetch("/config-push/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ devices }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      resultsEl.textContent = `Couldn't start the push: ${typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)}`;
      return;
    }
    await followPushJob(body.job_id);
  } catch (err) {
    resultsEl.textContent = `Request failed: ${err}`;
  } finally {
    button.disabled = false;
  }
});

// Drag #panel-resize-handle up/down to reallocate space between
// Topology Summary and the Console/Screen area below it.
(function setupPanelResize() {
  const handle = document.getElementById("panel-resize-handle");
  const topPanel = document.getElementById("topology-summary");
  let startY = 0;
  let startHeight = 0;

  function onMove(evt) {
    const dy = evt.clientY - startY;
    const min = 80;
    const max = document.getElementById("right-panel").clientHeight - 120;
    topPanel.style.height = `${Math.max(min, Math.min(startHeight + dy, max))}px`;
  }
  function onUp() {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    handle.classList.remove("dragging");
  }
  handle.addEventListener("pointerdown", (evt) => {
    startY = evt.clientY;
    startHeight = topPanel.getBoundingClientRect().height;
    handle.classList.add("dragging");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
})();

// Drag #right-panel-resize-handle left/right to resize the whole right
// panel (Topology Summary + Console/Screen together) - #push-area
// shares the same --right-panel-width custom property since it slots
// into the exact same spot when the View menu switches to it.
(function setupRightPanelResize() {
  const handle = document.getElementById("right-panel-resize-handle");
  const rightPanel = document.getElementById("right-panel");
  let startX = 0;
  let startWidth = 0;

  function onMove(evt) {
    const dx = startX - evt.clientX; // dragging left (toward canvas) widens the right panel
    const min = 220;
    const max = window.innerWidth - 420;
    const newWidth = Math.max(min, Math.min(startWidth + dx, max));
    document.documentElement.style.setProperty("--right-panel-width", `${newWidth}px`);
  }
  function onUp() {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    handle.classList.remove("dragging");
  }
  handle.addEventListener("pointerdown", (evt) => {
    startX = evt.clientX;
    startWidth = rightPanel.getBoundingClientRect().width;
    handle.classList.add("dragging");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
})();

// Pop the whole Console/Screen area out into its own real browser
// window - moves the actual live DOM subtree (not a copy), so every
// open xterm.js terminal/VNC screen and its WebSocket keeps running
// exactly as it was, uninterrupted. Moves it back into the right
// panel automatically if that window is closed.
// The button moves into the pop-out window with the tab row, so it is held here instead of looked up by id.
const popOutButton = document.getElementById("popout-btn");
function showPopOutState(out) {
  popOutButton.textContent = out ? "\u29c9 Pop in" : "\u29c9 Pop out";
  popOutButton.title = out
    ? "Put the Console/Screen view back into the side panel"
    : "Pop this panel out into its own window (Shift-click: a new browser tab)";
}

function popInPanel() {
  const win = poppedOutWindow;
  if (!win) return;
  const home = document.getElementById("console-area");
  for (const id of POP_OUT_PARTS) {
    const part = win.document.getElementById(id);
    if (part) home.appendChild(part);
  }
  document.getElementById("right-panel").classList.remove("popped-out");
  showPopOutState(false);
  poppedOutWindow = null;
}

popOutButton.addEventListener("click", (evt) => {
  if (poppedOutWindow && !poppedOutWindow.closed) {
    const win = poppedOutWindow;
    popInPanel();
    win.close();
    return;
  }
  const win = openPopOut(evt.shiftKey, "width=900,height=650");
  if (!win) {
    showAlert("Couldn't open a new window or tab - check your browser's popup blocker.");
    return;
  }
  win.document.head.innerHTML = document.head.innerHTML; // first: it also carries this page's own <title>
  win.document.title = "Ginnungagap - Console/Screen View";
  // the same structure as in the panel, so every existing style applies
  const area = win.document.createElement("div");
  area.id = "console-area";
  win.document.body.appendChild(area);
  for (const id of POP_OUT_PARTS) area.appendChild(document.getElementById(id));
  poppedOutWindow = win;
  // the side panel is left with the device list only
  document.getElementById("right-panel").classList.add("popped-out");
  showPopOutState(true);

  win.addEventListener("beforeunload", () => {
    if (poppedOutWindow === win) popInPanel();
  });
});

showLandingView();
initPalette();

// The template service decides whether any topology validates at all; when
// it is down or lists nothing, every error reads "Image X not found" - so
// the real problem is shown on every screen instead of being guessed at.
const TEMPLATE_HEALTH_INTERVAL_MS = 60000;
async function refreshTemplateBanner() {
  const banner = document.getElementById("template-banner");
  try {
    const resp = await fetch("/templates/health");
    if (!resp.ok) return;
    const health = await resp.json();
    document.getElementById("template-banner-text").textContent = health.ok ? "" : gns3VmProgress || `Template service problem: ${health.problems.join(" ")}`;
    banner.hidden = health.ok;
    // Offered while the GNS3 VM is switched off (see app/gns3_vm_power.py).
    const vmBtn = document.getElementById("template-gns3vm-btn");
    templateGns3Vm = !health.ok && health.gns3_vm && health.gns3_vm.action ? health.gns3_vm : null;
    vmBtn.hidden = !templateGns3Vm;
    if (templateGns3Vm && !vmBtn.disabled) {
      vmBtn.textContent = gns3VmButtonLabel(templateGns3Vm.action);
    }
    // Offered while at least one template service answers: they read their sources only on a timer.
    const refreshBtn = document.getElementById("template-refresh-btn");
    refreshBtn.hidden = health.ok || !health.refreshable;
  } catch (err) {
    // Backend not reachable right now - the other screens already say so.
  }
}
refreshTemplateBanner();
setInterval(refreshTemplateBanner, TEMPLATE_HEALTH_INTERVAL_MS);
