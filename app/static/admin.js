// [admin.js] Stand-alone admin panel (admin.html) - user accounts and the
// AI snippet topic allow-list (app/routes/admin.py). Deliberately small
// and self-contained, not part of the main canvas app's shared scripts.

async function request(url, options = {}) {
  const resp = await fetch(url, options);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${resp.status})`);
  return body;
}

let allProfiles = [];

async function loadProfiles() {
  try {
    allProfiles = await request("/settings-profiles");
  } catch {
    allProfiles = [];
  }
}

function hostsSummary(allowedHosts) {
  if (allowedHosts === "ALL") return "ALL";
  return allowedHosts.length ? allowedHosts.join(", ") : "(none)";
}

// A reusable "All hosts" checkbox plus one checkbox per existing settings
// profile - used for the create-user form, the bulk-import form, and each
// per-user row's edit popover. `getValue()` returns "ALL" or the list of
// filenames currently checked.
function makeHostsControl(initial) {
  const wrap = document.createElement("span");
  wrap.className = "ap-hosts-control";

  const allLabel = document.createElement("label");
  const allCheckbox = document.createElement("input");
  allCheckbox.type = "checkbox";
  allCheckbox.checked = initial === "ALL" || initial === undefined;
  allLabel.append(allCheckbox, document.createTextNode(" All hosts"));
  wrap.appendChild(allLabel);

  const hostCheckboxes = [];
  for (const profile of allProfiles) {
    const label = document.createElement("label");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = profile.filename;
    checkbox.checked = Array.isArray(initial) && initial.includes(profile.filename);
    checkbox.disabled = allCheckbox.checked;
    label.append(checkbox, document.createTextNode(" " + profile.filename));
    wrap.appendChild(label);
    hostCheckboxes.push(checkbox);
  }
  allCheckbox.addEventListener("change", () => {
    for (const checkbox of hostCheckboxes) checkbox.disabled = allCheckbox.checked;
  });

  return {
    element: wrap,
    getValue() {
      if (allCheckbox.checked) return "ALL";
      return hostCheckboxes.filter((checkbox) => checkbox.checked).map((checkbox) => checkbox.value);
    },
  };
}

function buildHostsCell(user) {
  const cell = document.createElement("td");
  if (user.role === "admin") {
    cell.textContent = "ALL";
    cell.title = "Admins always have access to every host";
    return cell;
  }
  cell.className = "ap-hosts-cell";

  const summary = document.createElement("span");
  summary.className = "ap-hosts-summary";
  summary.textContent = hostsSummary(user.allowed_hosts);

  const popover = document.createElement("div");
  popover.className = "ap-hosts-popover";
  const control = makeHostsControl(user.allowed_hosts);
  const saveBtn = document.createElement("button");
  saveBtn.className = "toolbar-btn primary";
  saveBtn.type = "button";
  saveBtn.textContent = "Save";
  saveBtn.addEventListener("click", async () => {
    try {
      const value = control.getValue();
      await request(`/admin/users/${encodeURIComponent(user.username)}/allowed-hosts`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ allowed_hosts: value }),
      });
      summary.textContent = hostsSummary(value);
      popover.classList.remove("ap-open");
      setStatus("ap-users-status", "");
    } catch (error) {
      setStatus("ap-users-status", error.message, true);
    }
  });
  popover.append(control.element, document.createElement("hr"), saveBtn);
  summary.addEventListener("click", (evt) => {
    evt.stopPropagation();
    popover.classList.toggle("ap-open");
  });
  cell.append(summary, popover);
  return cell;
}

document.addEventListener("click", (evt) => {
  for (const popover of document.querySelectorAll(".ap-hosts-popover.ap-open")) {
    if (!popover.contains(evt.target)) popover.classList.remove("ap-open");
  }
});

async function loadUsers() {
  const users = await request("/admin/users");
  const body = document.getElementById("ap-users-body");
  body.innerHTML = "";
  for (const user of users) {
    const row = document.createElement("tr");

    const nameCell = document.createElement("td");
    nameCell.textContent = user.username;

    const roleCell = document.createElement("td");
    const roleSelect = document.createElement("select");
    for (const role of ["user", "readonly", "admin"]) {
      const option = new Option(role, role, false, role === user.role);
      roleSelect.add(option);
    }
    roleSelect.addEventListener("change", async () => {
      try {
        await request(`/admin/users/${encodeURIComponent(user.username)}/role`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role: roleSelect.value }),
        });
        setStatus("ap-users-status", "");
        await loadUsers();
      } catch (error) {
        setStatus("ap-users-status", error.message, true);
        roleSelect.value = user.role;
      }
    });
    roleCell.appendChild(roleSelect);

    row.append(nameCell, roleCell, buildHostsCell(user));

    const actionCell = document.createElement("td");
    const deleteBtn = document.createElement("button");
    deleteBtn.className = "toolbar-btn";
    deleteBtn.type = "button";
    deleteBtn.textContent = "Delete";
    deleteBtn.addEventListener("click", async () => {
      try {
        await request(`/admin/users/${encodeURIComponent(user.username)}`, { method: "DELETE" });
        await loadUsers();
      } catch (error) {
        setStatus("ap-users-status", error.message, true);
      }
    });
    actionCell.appendChild(deleteBtn);
    row.appendChild(actionCell);

    body.appendChild(row);
  }
}

function setStatus(elementId, text, isError = false) {
  const el = document.getElementById(elementId);
  el.textContent = text;
  el.classList.toggle("ap-error", isError);
}

let newUserHostsControl = makeHostsControl("ALL");
let bulkHostsControl = makeHostsControl("ALL");

function refreshCreationHostsControls() {
  newUserHostsControl = makeHostsControl("ALL");
  const newUserSlot = document.getElementById("ap-new-hosts-control");
  newUserSlot.innerHTML = "";
  newUserSlot.appendChild(newUserHostsControl.element);

  bulkHostsControl = makeHostsControl("ALL");
  const bulkSlot = document.getElementById("ap-bulk-hosts-control");
  bulkSlot.innerHTML = "";
  bulkSlot.appendChild(bulkHostsControl.element);
}

document.getElementById("ap-create-user").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const username = document.getElementById("ap-new-username").value.trim();
  const password = document.getElementById("ap-new-password").value;
  const role = document.getElementById("ap-new-role").value;
  const allowedHosts = newUserHostsControl.getValue();
  try {
    await request("/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, role, allowed_hosts: allowedHosts }),
    });
    evt.target.reset();
    refreshCreationHostsControls();
    setStatus("ap-users-status", "");
    await loadUsers();
  } catch (error) {
    setStatus("ap-users-status", error.message, true);
  }
});

document.getElementById("ap-bulk-create").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const lines = document.getElementById("ap-bulk-lines").value;
  const role = document.getElementById("ap-bulk-role").value;
  const allowedHosts = bulkHostsControl.getValue();
  const resultsEl = document.getElementById("ap-bulk-results");
  resultsEl.innerHTML = "";
  setStatus("ap-bulk-status", "Importing…");
  try {
    const results = await request("/admin/users/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lines, role, allowed_hosts: allowedHosts }),
    });
    for (const result of results) {
      const item = document.createElement("li");
      item.textContent =
        result.status === "created"
          ? `✓ ${result.username}`
          : `✗ ${result.username || "(unreadable line)"} - ${result.detail}`;
      item.classList.toggle("ap-error", result.status !== "created");
      resultsEl.appendChild(item);
    }
    const createdCount = results.filter((r) => r.status === "created").length;
    setStatus("ap-bulk-status", `${createdCount} of ${results.length} account(s) created.`);
    evt.target.reset();
    refreshCreationHostsControls();
    await loadUsers();
  } catch (error) {
    setStatus("ap-bulk-status", error.message, true);
  }
});

const aiConfigForm = document.getElementById("ap-ai-config-form");
const aiConfigEnabled = document.getElementById("ap-ai-config-enabled");
const aiConfigSave = aiConfigForm.querySelector("button");

function showAiConfig({ enabled, available }, saved = false) {
  aiConfigEnabled.checked = enabled;
  const status = [saved ? "Saved." : "", available ? "" : "The configuration generator is not configured."];
  setStatus("ap-ai-config-status", status.filter(Boolean).join(" "));
}

async function loadAiConfig() {
  try {
    showAiConfig(await request("/admin/ai-config"));
    aiConfigEnabled.disabled = aiConfigSave.disabled = false;
  } catch (error) {
    setStatus("ap-ai-config-status", error.message, true);
  }
}

aiConfigForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  aiConfigEnabled.disabled = aiConfigSave.disabled = true;
  try {
    showAiConfig(await request("/admin/ai-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: aiConfigEnabled.checked }),
    }), true);
  } catch (error) {
    setStatus("ap-ai-config-status", error.message, true);
  } finally {
    aiConfigEnabled.disabled = aiConfigSave.disabled = false;
  }
});

const summarizeBtn = document.getElementById("ap-logs-summarize");
summarizeBtn.addEventListener("click", async () => {
  const summaryEl = document.getElementById("ap-logs-summary");
  summarizeBtn.disabled = true;
  summaryEl.hidden = true;
  setStatus("ap-logs-status", "Summarizing the last 2,000 log lines with AI - this can take a while…");
  try {
    const { summary } = await request("/admin/logs/summarize", { method: "POST" });
    summaryEl.textContent = summary;
    summaryEl.hidden = false;
    setStatus("ap-logs-status", "");
  } catch (error) {
    setStatus("ap-logs-status", error.message, true);
  } finally {
    summarizeBtn.disabled = false;
  }
});

async function loadAuditLog() {
  try {
    const events = await request("/admin/audit-log");
    const body = document.getElementById("ap-audit-body");
    body.innerHTML = "";
    for (const event of events) {
      const row = document.createElement("tr");
      const time = new Date(event.ts * 1000).toLocaleString();
      for (const value of [time, event.event, event.username, event.detail || ""]) {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.appendChild(cell);
      }
      body.appendChild(row);
    }
    setStatus("ap-audit-status", events.length ? "" : "No audit events yet.");
  } catch (error) {
    setStatus("ap-audit-status", error.message, true);
  }
}

(async () => {
  try {
    const me = await request("/me");
    if (me.role !== "admin") {
      document.getElementById("ap-content").style.display = "none";
      document.getElementById("ap-denied").style.display = "block";
      return;
    }
    await loadProfiles();
    refreshCreationHostsControls();
    await Promise.all([loadUsers(), loadAiConfig(), loadAuditLog(), loadTemplateShare()]);
  } catch {
    window.location.href = "/login";
  }
})();
