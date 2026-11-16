// [admin.js] Stand-alone admin panel (admin.html) - user accounts and the
// AI snippet topic allow-list (app/routes/admin.py). Deliberately small
// and self-contained, not part of the main canvas app's shared scripts.

async function request(url, options = {}) {
  const resp = await fetch(url, options);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${resp.status})`);
  return body;
}

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
    for (const role of ["user", "admin"]) {
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
      } catch (error) {
        setStatus("ap-users-status", error.message, true);
        roleSelect.value = user.role;
      }
    });
    roleCell.appendChild(roleSelect);

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

    row.append(nameCell, roleCell, actionCell);
    body.appendChild(row);
  }
}

function setStatus(elementId, text, isError = false) {
  const el = document.getElementById(elementId);
  el.textContent = text;
  el.classList.toggle("ap-error", isError);
}

document.getElementById("ap-create-user").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const username = document.getElementById("ap-new-username").value.trim();
  const password = document.getElementById("ap-new-password").value;
  const role = document.getElementById("ap-new-role").value;
  try {
    await request("/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, role }),
    });
    evt.target.reset();
    setStatus("ap-users-status", "");
    await loadUsers();
  } catch (error) {
    setStatus("ap-users-status", error.message, true);
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

(async () => {
  try {
    const me = await request("/me");
    if (me.role !== "admin") {
      document.getElementById("ap-content").style.display = "none";
      document.getElementById("ap-denied").style.display = "block";
      return;
    }
    await Promise.all([loadUsers(), loadAiConfig()]);
  } catch {
    window.location.href = "/login";
  }
})();
