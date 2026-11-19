// [admin-updates.js] The admin panel's Updates section: what is installed, "Check for updates" and a confirmed "Update now".
// The updater (Idynoun) runs on the appliance's host; the page only reads what it left (GET /admin/updates) and leaves one
// request for it (POST /admin/updates/check|apply, app/routes/updates.py). Loaded before admin.js, which calls loadUpdates().
// While the web app itself is restarted by an update, requests fail for a few seconds: the page keeps asking with a back-off,
// and the "Connection lost" overlay (connection-monitor.js) covers the screen meanwhile and reloads the page when the new
// version answers.

const updatesURL = "/admin/updates";
const UPDATES_POLL_MS = 2000;
const UPDATES_BACKOFF_MS = [2000, 4000, 8000, 15000];
let updatesView = null;
let updatesTimer = null;
let updatesFailures = 0;
let updatesWaiting = false; // an update was running when the last answer came: a failed request then means "restarting"
let updatesExpect = null; // { id, until }: a request just left; until its result shows, the page counts as busy

const updatesEl = (id) => document.getElementById(id);

function updatesTime(stamp) {
  const date = stamp ? new Date(stamp) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : "";
}

function updatesSchedule(milliseconds) {
  if (updatesTimer) clearTimeout(updatesTimer);
  updatesTimer = milliseconds > 0 ? setTimeout(loadUpdates, milliseconds) : null;
}

function renderUpdateRows(components, checked) {
  const body = updatesEl("ap-updates-body");
  body.replaceChildren();
  for (const row of components) {
    const tr = document.createElement("tr");
    const name = tr.insertCell();
    name.textContent = row.name;
    const role = document.createElement("span");
    role.className = "ap-updates-role";
    role.textContent = ` ${row.role}`;
    name.append(role);
    tr.insertCell().textContent = row.installed || "-";
    tr.insertCell().textContent = row.available || (checked ? "-" : "not checked");
    const kind = !row.change || row.change === "same" ? "-" : row.change === "skipped" ? "not selected" : row.change;
    tr.insertCell().textContent = kind;
    tr.insertCell().textContent = row.services.join(", ") || "-";
    if (kind !== "-" && kind !== "not selected") tr.className = "ap-updates-changed";
    body.append(tr);
  }
}

function updatesCheckLine(view) {
  const check = view.check;
  if (!check) return { text: "No check has been made yet.", error: false };
  if (check.error) return { text: `The last check failed: ${check.error}`, error: true };
  if (check.stale) return { text: "The installed release changed after the last check. Check for updates again.", error: false };
  const when = updatesTime(check.checked_at);
  const source = check.source ? ` Source: ${check.source}.` : "";
  const head = when ? `Checked ${when}.` : "Checked.";
  if (!check.update_available) return { text: `${head} Everything is up to date.${source}`, error: false };
  const signature = check.signature ? ` Signature: ${check.signature}.` : "";
  return { text: `${head} Release v${check.version} is available (${check.channel || "stable"}).${signature}${source}`, error: false };
}

function updatesResultLine(result) {
  if (!result || result.state === "running") return null;
  const who = result.requested_by ? ` Requested by ${result.requested_by}.` : "";
  const when = updatesTime(result.finished);
  const what = result.action === "apply" ? "Update" : "Check";
  return {
    text: `${what}${when ? ` of ${when}` : ""}: ${result.message}${who}`,
    error: result.state !== "done",
  };
}

function renderUpdateProgress(view, busy) {
  const wrap = updatesEl("ap-updates-progress-wrap");
  const bar = updatesEl("ap-updates-progress");
  wrap.hidden = !busy;
  if (!busy) return;
  const progress = view.progress;
  if (progress) {
    bar.max = progress.steps;
    bar.value = progress.step || 0;
    const target = progress.to ? ` to v${progress.to}` : "";
    const step = progress.step ? ` (step ${progress.step} of ${progress.steps})` : "";
    const who = progress.requested_by ? `, requested by ${progress.requested_by.replace(/^web:/, "")}` : "";
    setStatus("ap-updates-phase", `${progress.label}${step}. Update${target}${who}.`, progress.failed);
  } else if (!view.busy || view.request_pending) {
    bar.removeAttribute("value");
    setStatus("ap-updates-phase", "Waiting for the appliance to pick up the request...");
  } else {
    bar.removeAttribute("value");
    setStatus("ap-updates-phase", "The appliance is working on the request...");
  }
  if (view.request_pending && view.request_pending.overdue) {
    setStatus("ap-updates-phase", "The appliance has not picked up the request. Its update service may not be running.", true);
  }
}

function renderUpdates(view) {
  updatesView = view;
  const card = updatesEl("ap-updates-card");
  if (!view.enabled) {
    card.hidden = true;
    setStatus("ap-updates-unavailable", view.reason, false);
    return;
  }
  card.hidden = false;
  setStatus("ap-updates-unavailable", "");
  const release = view.release;
  const installed = release.adopted
    ? "Installed: the components that were on this machine (no release recorded)."
    : release.version
      ? `Installed: appliance release v${release.version}${release.installed_at ? `, ${updatesTime(release.installed_at)}` : ""}.`
      : "Installed: no release recorded.";
  updatesEl("ap-updates-release").textContent = installed;
  renderUpdateRows(view.components, Boolean(view.check && !view.check.error));
  const awaiting = Boolean(updatesExpect && Date.now() < updatesExpect.until && !(view.result && view.result.id === updatesExpect.id));
  if (!awaiting) updatesExpect = null;
  const busy = view.busy || awaiting;
  renderUpdateProgress(view, busy);
  // a finished check is told by the check line (it names the release and its signature); everything else by its own result
  const result = view.result && view.result.state === "done" && view.result.action === "check" ? null : view.result;
  const line = busy ? null : updatesResultLine(result) || updatesCheckLine(view);
  if (busy) {
    setStatus("ap-updates-status", "");
  } else if (line) {
    setStatus("ap-updates-status", line.text, line.error);
  }
  updatesEl("ap-updates-check").disabled = busy;
  const apply = updatesEl("ap-updates-apply");
  apply.hidden = !view.can_apply || awaiting;
  if (view.can_apply) apply.textContent = `Update to v${view.check.version}…`;
  const log = updatesEl("ap-updates-log-card");
  const lines = view.result && view.result.log ? view.result.log : [];
  log.hidden = !lines.length;
  updatesEl("ap-updates-log").textContent = lines.join("\n");
  view.pollSoon = busy;
  updatesWaiting = busy;
}

async function loadUpdates() {
  try {
    const view = await request(updatesURL);
    updatesFailures = 0;
    renderUpdates(view);
    updatesSchedule(view.pollSoon ? UPDATES_POLL_MS : 0);
  } catch (error) {
    if (!updatesWaiting) {
      setStatus("ap-updates-status", error.message, true);
      return;
    }
    updatesFailures += 1;
    updatesEl("ap-updates-progress-wrap").hidden = false;
    updatesEl("ap-updates-progress").removeAttribute("value");
    if (error instanceof TypeError) {
      // an update is under way and the web app is not answering at all: it is being restarted. Keep asking, slowly.
      setStatus("ap-updates-phase", "The web app is restarting. This page reconnects by itself.");
    } else {
      setStatus("ap-updates-phase", `${error.message} Trying again.`, true);
    }
    updatesSchedule(UPDATES_BACKOFF_MS[Math.min(updatesFailures, UPDATES_BACKOFF_MS.length) - 1]);
  }
}

async function postUpdateRequest(path, payload) {
  const options = { method: "POST" };
  if (payload) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(payload);
  }
  return request(`${updatesURL}/${path}`, options);
}

updatesEl("ap-updates-check").addEventListener("click", async () => {
  updatesEl("ap-updates-check").disabled = true;
  setStatus("ap-updates-status", "Asking the appliance to look for a new release...");
  try {
    const { id } = await postUpdateRequest("check");
    updatesExpect = { id, until: Date.now() + 30000 };
    updatesWaiting = true;
    await loadUpdates();
  } catch (error) {
    setStatus("ap-updates-status", error.message, true);
    updatesEl("ap-updates-check").disabled = false;
  }
});

function openUpdateDialog() {
  const view = updatesView;
  if (!view || !view.can_apply) return;
  const check = view.check;
  updatesEl("ap-updates-dialog-title").textContent = `Install release v${check.version}?`;
  const plan = updatesEl("ap-updates-plan");
  plan.replaceChildren();
  const services = new Set();
  for (const row of view.components) {
    if (!row.change || row.change === "same" || row.change === "skipped") continue;
    const item = document.createElement("li");
    item.textContent = `${row.name} ${row.installed || "-"} to ${row.available} (${row.change})`;
    plan.append(item);
    row.services.forEach((service) => services.add(service));
  }
  const notes = [];
  if (services.size) notes.push(`Restarts: ${[...services].join(", ")}.`);
  if (view.components.some((row) => row.restarts_web_app)) {
    notes.push("The web app restarts for about 10 to 20 seconds. This page reconnects by itself.");
  }
  notes.push(check.data_migration ? "This release changes stored data; a backup is made first." : "A backup is made first.");
  notes.push("If something is not healthy afterwards, the appliance goes back to the release it ran before.");
  updatesEl("ap-updates-dialog-notes").replaceChildren(...notes.map((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    return item;
  }));
  updatesEl("ap-updates-install").textContent = `Install v${check.version} now`;
  setStatus("ap-updates-dialog-status", "");
  updatesEl("ap-updates-dialog").showModal();
}

updatesEl("ap-updates-apply").addEventListener("click", openUpdateDialog);
updatesEl("ap-updates-cancel").addEventListener("click", () => updatesEl("ap-updates-dialog").close());
updatesEl("ap-updates-install").addEventListener("click", async () => {
  const version = updatesView && updatesView.check ? updatesView.check.version : null;
  if (!version) return;
  const button = updatesEl("ap-updates-install");
  button.disabled = true;
  try {
    const { id } = await postUpdateRequest("apply", { version, confirm: true });
    updatesEl("ap-updates-dialog").close();
    updatesExpect = { id, until: Date.now() + 30000 };
    updatesWaiting = true;
    setStatus("ap-updates-status", "");
    await loadUpdates();
  } catch (error) {
    setStatus("ap-updates-dialog-status", error.message, true);
  } finally {
    button.disabled = false;
  }
});

// the overlay of connection-monitor.js reports the server as reachable again: ask for the state at once
if (window.connectionMonitor && typeof window.connectionMonitor.onRestored === "function") {
  window.connectionMonitor.onRestored(() => loadUpdates());
}
