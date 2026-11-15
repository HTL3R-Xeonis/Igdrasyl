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

async function loadAiTopics() {
  const { topics, enabled } = await request("/admin/ai-snippet-topics");
  const form = document.getElementById("ap-topics-form");
  form.innerHTML = "";
  for (const [id, label] of Object.entries(topics)) {
    const wrap = document.createElement("label");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = id;
    checkbox.checked = enabled.includes(id);
    wrap.append(checkbox, ` ${label}`);
    form.appendChild(wrap);
  }
}

document.getElementById("ap-topics-save").addEventListener("click", async () => {
  const topics = [...document.querySelectorAll("#ap-topics-form input:checked")].map((el) => el.value);
  try {
    await request("/admin/ai-snippet-topics", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topics }),
    });
    setStatus("ap-topics-status", "Saved.");
  } catch (error) {
    setStatus("ap-topics-status", error.message, true);
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
    await Promise.all([loadUsers(), loadAiTopics()]);
  } catch {
    window.location.href = "/login";
  }
})();
